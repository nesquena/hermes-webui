"""#8613 round 3 (maintainer, 2026-11-02): the three blocking findings of the
release gate on ``8e49511d``.

The gate re-ran the release checks against the installed Agent's real
``MemoryManager``, a provider with a real ``httpx.Client`` and the real
eviction entry point, and reported three new problems. Each test here
reproduces ONE of them deterministically, so the fix can be proved by the same
test turning green.

A. A logging handler's lock and the holder lock deadlock against each other
   through a GC weak-reference callback.
B. A GC during registration erases a LIVE agent's registration.
C. The evicted agent's OWN pending save is lost.

A and B run the production constructor and the real eviction entry point, so
the holder table is populated exactly the way a turn populates it. The oracle
in A is that both threads finish and a later memory-bearing construction still
works; in B and C it is the SURVIVING save: ``httpx`` refuses a request on a
closed client, so a release that ran under a queued save fails it.
"""
# ruff: noqa: F811  (pytest fixtures imported from the #8517 P0 module)
from __future__ import annotations

import gc
import json
import os
import subprocess
import sys
import time

import pytest

from tests.test_issue8517_profile_scoped_memory import (  # noqa: F401 (fixtures)
    _key,
    hc,
    homes,
)
from tests.test_issue8613_provider_instance_holders import (  # noqa: F401
    PROVIDER_NAME,
    SharedInstanceProvider,
    _agent_home,
    _build,
    _fresh_instance,
    _pause_saves,
    _queue_paused_save,
    _shared_agent,
    holder_registry,
)

import api.memory_profile_scope as scope
import api.streaming as streaming

mm_mod = pytest.importorskip("agent.memory_manager")
if not callable(getattr(getattr(mm_mod, "MemoryManager", None), "release_soft", None)):
    pytest.skip("installed Agent predates MemoryManager.release_soft", allow_module_level=True)


# ── A. the logging handler's lock deadlocks against the holder lock ────────
#
# A weak-reference callback runs wherever the collected object dies — inside a
# GC, on whatever thread triggered it. The callback that forgets a collected
# agent takes the holder lock, and the thread that holds the holder lock may be
# LOGGING: a logging handler holds its own lock while it formats and writes,
# and any allocation on that thread can run a GC.
#
# So thread W holds the handler lock and collects a registered agent (its
# callback waits for the holder lock), while thread E holds the holder lock
# and logs (it waits for the handler lock). Neither ever runs again.
#
# The probe drives exactly that schedule: the handler's write() collects the
# agent while the evictor holds the holder lock and logs.


class _CollectingStream:
    """A log stream whose write() runs a GC — the seam that starts the cycle."""

    def __init__(self, on_write):
        self._on_write = on_write

    def write(self, text):
        self._on_write(text)

    def flush(self):
        pass


def _run_deadlock_probe(mode: str, timeout: float = 30.0) -> dict:
    """Run the logging/GC deadlock probe in a SUBPROCESS and report its verdict.

    The probe deliberately deadlocks on the unfixed tree. Running it in a
    child process keeps that deadlock out of the test session (a deadlocked
    thread holds the holder lock forever and would wedge every later test in
    the same process), and the child is killed on a timeout instead of hanging
    the suite. The child prints one JSON line: the two threads' aliveness and
    their stacks.

    ``mode`` selects which of the two lock-ordering sites the probe hits:
    ``"nowait"`` (the release refuses a fence wait without logging under the
    holder lock) and ``"timeout"`` (the fence wait times out and logs under it).
    """
    probe = r'''
import faulthandler, gc, json, logging, os, sys, threading

sys.path.insert(0, os.environ.get("HERMES_AGENT_DIR", os.path.expanduser("~/.hermes/hermes-agent")))
import api.memory_profile_scope as scope
from agent.memory_manager import MemoryManager

gc.disable()

MODE = sys.argv[1]

class Provider:
    name = "gate-logging"

class Agent:
    def __init__(self):
        self._memory_manager = MemoryManager()
        self._memory_manager._providers = [Provider()]

home = os.environ["HERMES_HOME"]
agent = Agent()
victim = Agent()
if hasattr(scope, "register_provider_holders"):
    scope.register_provider_holders(agent, home_key=home)
    scope.register_provider_holders(victim, home_key=home)
    scope._reserve_acquisition(home)
victim.cycle = victim
del victim

handler_held = threading.Event()
holders_held = threading.Event()

class CollectingStream:
    def write(self, text):
        if "writer-record" in text:
            handler_held.set()
            assert holders_held.wait(3)
            gc.collect()
    def flush(self):
        pass

log = scope.logger
log.handlers[:] = [logging.StreamHandler(CollectingStream())]
log.propagate = False
log.setLevel(logging.INFO)

def writer():
    log.warning("writer-record")

def evict():
    assert handler_held.wait(3)
    if hasattr(scope, "_claim_last_held_teardown"):
        with scope._provider_holders_lock:
            holders_held.set()
            if MODE == "timeout":
                scope._ACQUISITION_FENCE_WAIT_S = 0
                scope._claim_last_held_teardown(agent)
            else:
                with scope.release_without_fence_wait():
                    scope._claim_last_held_teardown(agent)
    else:
        holders_held.set()
        log.info("master has no unowned release/fence or holder GC callback")

threads = [threading.Thread(target=writer, daemon=True),
           threading.Thread(target=evict, daemon=True)]
for thread in threads:
    thread.start()
for thread in threads:
    thread.join(2)
alive = [t.is_alive() for t in threads]
if any(alive):
    # The deadlock is the finding: dump every thread's stack (the collector
    # waits for the holder lock, the evictor waits for the handler lock) so the
    # report names BOTH sides of the cycle.
    faulthandler.dump_traceback(file=sys.stderr)
frames = sys._current_frames()
stacks = []
for thread in threads:
    frame = frames.get(thread.ident)
    stack = []
    while frame:
        stack.append("%s:%d:%s" % (os.path.basename(frame.f_code.co_filename),
                                   frame.f_lineno, frame.f_code.co_name))
        frame = frame.f_back
    stacks.append(stack)
print(json.dumps({"deadlocked": alive, "stacks": stacks}), flush=True)
os._exit(0)
'''
    # The child gets an explicit, minimal environment rather than a copy of
    # the parent's: an interpreter search path, HOME (the probe expands
    # ``~/.hermes/hermes-agent`` when HERMES_AGENT_DIR is unset), SYSTEMROOT on
    # Windows, the test's isolated HERMES_HOME and the repository on
    # PYTHONPATH. Nothing else from the test session reaches the probe.
    env = {
        name: os.environ[name]
        for name in ("PATH", "HOME", "SYSTEMROOT", "HERMES_AGENT_DIR")
        if name in os.environ
    }
    if "HERMES_AGENT_DIR" not in env:
        # The parent may have found the Agent through another candidate (a
        # sibling checkout); point the child at the same package.
        import agent.memory_manager as _agent_module
        env["HERMES_AGENT_DIR"] = os.path.dirname(os.path.dirname(os.path.abspath(_agent_module.__file__)))
    env["HERMES_HOME"] = (
        os.environ.get("HERMES_HOME") or os.environ.get("HERMES_WEBUI_TEST_STATE_DIR", "")
    )
    env["PYTHONPATH"] = os.pathsep.join(
        [p for p in (os.getcwd(), os.environ.get("PYTHONPATH", "")) if p]
    )
    started = time.monotonic()
    proc = subprocess.run(
        [sys.executable, "-c", probe, mode],
        capture_output=True, text=True, timeout=timeout, env=env, check=False,
    )
    payload = {"mode": mode, "elapsed": round(time.monotonic() - started, 2)}
    for line in proc.stdout.splitlines():
        line = line.strip()
        if line.startswith("{"):
            try:
                payload.update(json.loads(line))
            except ValueError:
                pass
    payload["stdout"] = proc.stdout
    payload["stderr"] = proc.stderr[-4000:]
    payload["returncode"] = proc.returncode
    return payload


@pytest.mark.parametrize("mode", ["nowait", "timeout"])
def test_a_logging_handler_lock_never_deadlocks_against_the_holder_lock(mode, hc, homes, holder_registry):
    """A (maintainer, r3). The logging handler's lock and the holder lock must
    not deadlock against each other through a GC weak-reference callback.

    The cycle: a thread holds the logging handler's lock and runs a GC whose
    callback waits for the holder lock, while another thread holds the holder
    lock and logs — which waits for the handler lock. After that, every
    memory-bearing construction blocks on the holder lock, and only a restart
    recovers.

    The probe runs in a child process (the unfixed tree deadlocks FOREVER in
    that child, and the deadlocked thread holds the holder lock, which would
    wedge every later test in this process). The child prints both threads'
    aliveness; the fix must leave both finished.
    """
    result = _run_deadlock_probe(mode)
    assert result.get("deadlocked") == [False, False], (
        "the logging handler lock and the holder lock deadlocked against each "
        f"other (mode={mode!r}); the probe's threads are still alive.\n"
        f"stacks: {json.dumps(result.get('stacks'), indent=2)}\n"
        f"stderr: {result.get('stderr', '')[-2000:]}"
    )

    # The threads are not enough: the holder lock must be USABLE afterwards.
    # A deadlocked evictor holds it forever, so the next memory-bearing
    # construction would hang — which is the failure the maintainer described.
    provider = SharedInstanceProvider()
    agent = _shared_agent(homes["alpha"], provider, "8613-r3-after-deadlock")
    assert agent is not None
    assert agent._memory_manager is not None, "the memory agent was not built"
    assert streaming._close_evicted_agent_at_session_boundary(
        "8613-r3-after-deadlock", agent
    ) is True
    assert provider.shutdowns == 1, "the provider was not released after the deadlock probe"


# ── B. a GC during registration erases a live agent's registration ─────────
#
# `register_provider_holders` publishes the new entry and only afterwards
# allocates the agent's weak reference. That allocation can run a GC, whose
# callbacks prune the table PROCESS-WIDE — and the entry just published is
# still EMPTY, so the prune deletes it. The registration then fills a detached
# object: the live agent is not in the table, so its next save goes through a
# client another agent's teardown closed.
#
# The probe forces the collection at exactly that allocation point.


def test_b_a_gc_during_registration_does_not_erase_a_live_holder(hc, homes, holder_registry, monkeypatch):
    """B (maintainer, r3). A live agent's registration must survive a GC that
    runs while it is being registered.

    The oracle is the surviving save: after another agent of the same profile
    is evicted, the live agent's next save must still go through the shared
    client.
    """
    was_enabled = gc.isenabled()
    gc.disable()
    try:
        # A collected agent, registered, so its weak-reference callback runs
        # during the collection below and prunes the table.
        victim = _shared_agent(homes["alpha"], SharedInstanceProvider(), "gate-gc-victim")
        type(victim).built.clear()
        victim.cycle = victim
        del victim

        provider = SharedInstanceProvider()
        if hasattr(scope, "_weak_agent_ref"):
            original = scope._weak_agent_ref

            def gc_at_weakref_allocation(agent):
                gc.collect()
                return original(agent)

            with monkeypatch.context() as patch:
                patch.setattr(scope, "_weak_agent_ref", gc_at_weakref_allocation)
                first = _shared_agent(homes["alpha"], provider, "gate-gc-first")
            assert id(provider) in scope._provider_holders, (
                "the live agent's registration was erased by the GC that ran "
                "during its registration"
            )
        else:
            gc.collect()
            first = _shared_agent(homes["alpha"], provider, "gate-gc-first")

        second = _shared_agent(homes["alpha"], provider, "gate-gc-second")
        first.save("before eviction")
        assert streaming._close_evicted_agent_at_session_boundary("gate-gc-second", second)
        # ORACLE: the live holder's next save still goes through the client.
        first.save("live holder after eviction")
        assert provider.writes == ["before eviction", "live holder after eviction"]
    finally:
        if was_enabled:
            gc.enable()


# ── C. the evicted agent's own pending save is lost ────────────────────────
#
# The last-holder proof exempts the RELEASING manager's own outstanding work
# ("the release drains that itself"). But the installed Agent's
# `release_soft()` closes the providers after BOUNDED waits (10 s flush, 5 s
# drain): a sole agent whose save is still running past those waits loses it,
# and the provider client is closed under it.
#
# The probe is the real eviction entry point with a REAL paused save on the
# evicted agent's own manager.


class _NativeClientProvider(SharedInstanceProvider):
    """A provider whose save really goes through the shared client."""

    def remember(self, marker):
        self.client.post("https://memory.provider/save", json={"marker": marker}).raise_for_status()
        self.writes.append(marker)


def test_c_the_sole_evicted_agents_own_pending_save_survives(hc, homes, holder_registry):
    """C (maintainer, r3). A sole evicted agent's OWN pending save must not be
    lost.

    The evicted agent is the only holder and its own manager has a queued save
    still running when the eviction lands. The release must not close the
    client under that save — the save must land.
    """
    provider = _pause_saves(_NativeClientProvider())
    agent = _shared_agent(homes["alpha"], provider, "gate-sole-pending")
    agent.session_id = "gate-sole-pending"
    _queue_paused_save(agent, provider, "sole pending save", hc)
    futures = list(agent._memory_manager._background_futures)
    began = time.monotonic()
    try:
        assert streaming._close_evicted_agent_at_session_boundary(agent.session_id, agent)
        elapsed = round(time.monotonic() - began, 2)
        closed_before_save = provider.client.is_closed
        shutdowns = provider.shutdowns
    finally:
        provider.save_resume.set()
        for future in futures:
            future.result(timeout=30)
    # ORACLE: the pending save went through the client and landed.
    assert provider.writes == ["sole pending save"], (
        f"the sole evicted agent's own pending save was lost "
        f"(elapsed={elapsed}, closed_before_save={closed_before_save}, "
        f"shutdowns={shutdowns}, writes={provider.writes!r})"
    )
