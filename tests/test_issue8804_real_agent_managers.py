"""#8804 with the REAL memory managers of two Hermes Agent releases:

* v0.22.2: no ``claim_instance``, no ``MemoryManager.release_soft``/``claim_teardown``, no ``_bound_scope``.
  The release runs its ``shutdown_all``, and the WebUI's reservation fence is the only thing keeping a new
  agent off the instance while it runs.
* v0.22.7: all of them. The release runs ``release_soft``, exactly as on exp-v0.52.627.

Each version's ``agent/memory_manager.py`` and its ``AIAgent.shutdown_memory_provider`` (with
``_notify_context_engine_session_end``) are loaded from the Agent checkout's tags, or from
``$WEBUI_TEST_AGENT_TREES/<tag>/`` (an extracted tree). The module stands in for ``agent.memory_manager`` for
the test, so the WebUI's own seam detection sees exactly that Agent. A version that is not available is
SKIPPED with the reason, never faked; ``pytest -rs`` lists which ran.
"""
# ruff: noqa: F811  (pytest fixtures imported from the #8517 / #8613 modules)
from __future__ import annotations

import ast
import functools
import logging
import os
import subprocess
import sys
import threading
import types
import typing
from pathlib import Path

import pytest

pytest.importorskip("agent.memory_manager")

import api.memory_profile_scope as scope  # noqa: E402
import api.profiles as profiles_api  # noqa: E402
import api.streaming as streaming  # noqa: E402
from tests.test_issue8517_profile_scoped_memory import hc, homes  # noqa: E402,F401
from tests.test_issue8613_post_drain_release import RecordingProvider, _wait_for  # noqa: E402
from tests.test_issue8613_provider_instance_holders import _pause_saves, holder_registry  # noqa: E402,F401
from tests.test_issue8804_unfenced_teardown import FenceProbe  # noqa: E402

AGENT_VERSIONS = ("v0.22.2", "v0.22.7")


def _agent_checkout() -> "Path | None":
    try:
        import agent  # noqa: PLC0415
    except Exception:
        return None
    root = Path(agent.__file__).resolve().parent.parent
    return root if (root / ".git").exists() else None


def _tagged_source(tag: str, path: str) -> "str | None":
    trees = os.environ.get("WEBUI_TEST_AGENT_TREES")
    if trees:
        candidate = Path(trees) / tag / path
        if candidate.is_file():
            return candidate.read_text(encoding="utf-8")
    repo = _agent_checkout()
    if repo is None:
        return None
    try:
        out = subprocess.run(
            ["git", "-C", str(repo), "show", f"{tag}:{path}"], capture_output=True, text=True, timeout=60,
        )
    except Exception:
        return None
    return out.stdout if out.returncode == 0 and out.stdout else None


def _quietly(fn):
    try:
        fn()
    except Exception:  # pragma: no cover - the Agent's own helper logs and continues
        pass


@functools.lru_cache(maxsize=None)
def _load_agent_version(tag: str):
    """``(memory_manager module, AIAgent.shutdown_memory_provider)`` of Agent ``tag``,
    or the reason it cannot be loaded (a string, for ``pytest.skip``)."""
    mm_src = _tagged_source(tag, "agent/memory_manager.py")
    agent_src = _tagged_source(tag, "run_agent.py")
    if not mm_src or not agent_src:
        return (f"Hermes Agent {tag} is not available here (no tagged Agent checkout; set "
                f"WEBUI_TEST_AGENT_TREES to a directory holding {tag}/agent/memory_manager.py and {tag}/run_agent.py)")
    name = "webui_test_agent_memory_manager_" + tag.replace(".", "_")
    module = types.ModuleType(name)
    module.__file__ = f"<hermes-agent {tag}>/agent/memory_manager.py"
    sys.modules[name] = module
    try:
        exec(compile(mm_src, module.__file__, "exec"), module.__dict__)
    except Exception as exc:
        sys.modules.pop(name, None)
        return f"Hermes Agent {tag}'s agent/memory_manager.py does not load here ({type(exc).__name__}: {exc})"
    wanted = ("_notify_context_engine_session_end", "shutdown_memory_provider")
    found: dict = {}
    for node in ast.walk(ast.parse(agent_src)):
        if isinstance(node, ast.FunctionDef) and node.name in wanted:
            found.setdefault(node.name, node)
    if set(found) != set(wanted):
        return f"Hermes Agent {tag}'s run_agent.py has no {sorted(set(wanted) - set(found))}"
    namespace = {"_quietly": _quietly, "logger": logging.getLogger(f"hermes_agent_{tag}"),
                 "Any": typing.Any, "Optional": typing.Optional}
    for fn_name in wanted:
        exec(compile(ast.Module(body=[found[fn_name]], type_ignores=[]),
                     f"<hermes-agent {tag}>/run_agent.py", "exec"), namespace)
    return module, namespace["shutdown_memory_provider"]


@pytest.fixture(params=AGENT_VERSIONS)
def agent_version(request, hc, homes, holder_registry, monkeypatch):
    loaded = _load_agent_version(request.param)
    if isinstance(loaded, str):
        pytest.skip(loaded)
    module, shutdown_memory_provider = loaded
    monkeypatch.setitem(sys.modules, "agent.memory_manager", module)
    # A single-profile install: the root profile only, the one shape in which v0.22.2
    # keeps an external provider (no claim_instance, #8585).
    for name in ("alpha", "beta"):
        homes[name].rmdir()
    (homes["default"] / "profiles").rmdir()
    monkeypatch.setattr(profiles_api, "get_hermes_home_for_profile", lambda name: homes["default"])
    monkeypatch.setattr(profiles_api, "get_profile_runtime_env", lambda home: {}, raising=False)
    monkeypatch.setattr(profiles_api, "filter_runtime_env_for_gateway_parity", lambda env: {}, raising=False)
    monkeypatch.setattr(scope, "_profile_homes_seen", set())
    monkeypatch.setattr(scope, "_inflight_homes", {})
    seam = callable(getattr(module, "claim_instance", None))
    assert scope._claim_seam_available() is seam
    return types.SimpleNamespace(tag=request.param, module=module, shutdown=shutdown_memory_provider,
                                 root=homes["default"], seam=seam)


def _agent_cls(v, provider):
    class TaggedAgent:
        def __init__(self, session_id=None, skip_memory=False, **kwargs):
            self.session_id = session_id
            self._memory_manager = None
            self._memory_provider_shutdown = False
            self._session_messages = []
            self.context_compressor = None
            if not skip_memory:
                manager = v.module.MemoryManager()
                manager.add_provider(provider)
                home = str(sys.modules["hermes_constants"].get_hermes_home())
                manager.initialize_all(session_id=session_id, hermes_home=home)
                self._memory_manager = manager

        def shutdown_memory_provider(self, messages=None):
            v.shutdown(self, messages)

    return TaggedAgent


def _build(v, provider, session_id):
    agent, cacheable = scope.construct_profile_scoped_agent(
        _agent_cls(v, provider), {"session_id": session_id}, profile_home=str(v.root), session_id=session_id,
    )
    return agent, cacheable


def _count_manager_calls(agent):
    manager = agent._memory_manager
    calls = {"shutdown_all": 0, "on_session_end": 0}
    real_shutdown_all, real_on_session_end = manager.shutdown_all, manager.on_session_end

    def shutdown_all(*a, **k):
        calls["shutdown_all"] += 1
        return real_shutdown_all(*a, **k)

    def on_session_end(*a, **k):
        calls["on_session_end"] += 1
        return real_on_session_end(*a, **k)

    manager.shutdown_all, manager.on_session_end = shutdown_all, on_session_end
    return calls


def test_a_sole_unowned_eviction_stops_the_provider_once_and_never_ends_the_session(agent_version):
    v = agent_version
    provider = RecordingProvider()
    agent, cacheable = _build(v, provider, f"8804-{v.tag}-sole")
    assert cacheable is True and agent._memory_manager is not None, "the build kept no memory"
    calls = _count_manager_calls(agent)

    assert streaming._close_evicted_agent_at_session_boundary(f"8804-{v.tag}-sole", agent) is True
    agent.shutdown_memory_provider([])  # a later close()

    assert provider.shutdowns == 1 and provider.client.is_closed is True, f"{v.tag}: the writer outlived the eviction"
    assert provider.session_ends == 0 and calls["on_session_end"] == 0, f"{v.tag}: an unknown owner got a session end"
    assert provider.shutdown_homes == [str(v.root)], provider.shutdown_homes
    if not v.seam:
        assert calls["shutdown_all"] == 1, calls


def test_an_instance_shared_with_a_live_agent_keeps_running(agent_version):
    v = agent_version
    provider = RecordingProvider()
    first, _ = _build(v, provider, f"8804-{v.tag}-a")
    second, _ = _build(v, provider, f"8804-{v.tag}-b")

    assert streaming._close_evicted_agent_at_session_boundary(f"8804-{v.tag}-a", first) is True
    assert provider.shutdowns == 0 and provider.client.is_closed is False, f"{v.tag}: shut down under a live agent"
    provider.remember("the live agent's save")
    assert provider.writes == ["the live agent's save"]

    assert streaming._close_evicted_agent_at_session_boundary(f"8804-{v.tag}-b", second) is True
    assert provider.shutdowns == 1 and provider.session_ends == 0


def test_an_owned_eviction_keeps_the_agents_own_session_end(agent_version, monkeypatch):
    v = agent_version
    provider = RecordingProvider()
    agent, _ = _build(v, provider, f"8804-{v.tag}-owned")
    monkeypatch.setattr(scope, "session_owner_profile_home", lambda sid: str(v.root))

    assert streaming._close_evicted_agent_at_session_boundary(f"8804-{v.tag}-owned", agent) is True
    assert provider.session_ends == 1 and provider.shutdowns == 1


def test_a_build_during_an_old_agents_teardown_waits_and_gets_a_live_provider(agent_version):
    v = agent_version
    if v.seam:
        pytest.skip(f"{v.tag} refuses the instance itself (claim_instance); covered by the #8613 tests")
    provider = RecordingProvider()
    old, _ = _build(v, provider, "8804-race-old")
    in_teardown, finish = threading.Event(), threading.Event()
    real_shutdown = provider.shutdown

    def paused_shutdown():
        in_teardown.set()
        assert finish.wait(30), "the test never let the teardown finish"
        real_shutdown()

    provider.shutdown = paused_shutdown
    evicting = threading.Thread(target=streaming._close_evicted_agent_at_session_boundary,
                                args=("8804-race-old", old))
    evicting.start()
    built: dict = {}

    def build_new():
        built["agent"], built["cacheable"] = _build(v, provider, "8804-race-new")

    attaching = threading.Thread(target=build_new)
    try:
        assert in_teardown.wait(30), "the teardown never reached the provider"
        with FenceProbe(attaching) as fence:
            attaching.start()
            attaching.join(0.5)
            assert attaching.is_alive(), "the build did not wait for the teardown of the instance it shares"
            fence.stray_wake_inside_the_wait()
            assert attaching.is_alive(), "a stray wake-up let the build through while the teardown still runs"
        assert [i["session"] for i in provider.inits] == ["8804-race-old"], "re-initialised mid-teardown"
    finally:
        finish.set()
        evicting.join(30)
    assert not evicting.is_alive()
    # The teardown's settlement wakes the waiting build: it must not sit out its whole
    # bounded wait (30 s) before it notices the instance is free.
    attaching.join(5)
    assert not attaching.is_alive(), "the build was not woken when the teardown settled"

    assert [i["session"] for i in provider.inits] == ["8804-race-old", "8804-race-new"]
    assert provider.shutdowns == 1
    assert built["cacheable"] is True and built["agent"]._memory_manager is not None
    assert provider.client.is_closed is False
    provider.remember("the new agent's save")
    assert provider.writes == ["the new agent's save"]


def test_a_deferred_release_then_a_late_close_leaves_the_successor_its_provider(agent_version):
    """Codex r3's reproduction: queue a save, evict (the release waits for the save),
    let the deferred release run, build a successor sharing the provider, then close
    the departed agent. One shutdown, no session end, and the successor's client open."""
    v = agent_version
    provider = _pause_saves(RecordingProvider())
    old, _ = _build(v, provider, f"8804-{v.tag}-deferred-old")
    hc_mod = sys.modules["hermes_constants"]
    token = hc_mod.set_hermes_home_override(str(v.root))
    try:
        old._memory_manager.sync_all("the departed agent's save", "reply", session_id=old.session_id)
        assert provider.save_started.wait(30), "the queued save never reached the provider"
    finally:
        hc_mod.reset_hermes_home_override(token)
    with scope.release_without_fence_wait():
        assert streaming._close_evicted_agent_at_session_boundary(f"8804-{v.tag}-deferred-old", old) is True
    assert provider.shutdowns == 0
    provider.save_resume.set()
    assert provider.save_landed.wait(30)
    assert _wait_for(lambda: provider.shutdowns == 1, 30), f"{v.tag}: the deferred release never ran"

    successor, cacheable = _build(v, provider, f"8804-{v.tag}-deferred-new")
    assert cacheable is True and provider.client.is_closed is False
    old.shutdown_memory_provider([])  # the departed agent's own close, later

    assert provider.session_ends == 0, f"{v.tag}: the late close ran a session end under the unknown owner"
    assert provider.shutdowns == 1, f"{v.tag}: the late close shut the provider down a second time"
    assert provider.client.is_closed is False
    provider.remember("the successor's save")
    assert provider.writes[-1] == "the successor's save"
    assert successor._memory_manager is not None
