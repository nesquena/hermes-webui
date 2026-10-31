"""#8517 P4: one shared memory-provider instance must never be re-pointed across profiles.

Some memory providers (mnemosyne-hermes 0.7.1; the probe harness stand-in) hand every
agent the SAME process-wide instance. The Agent's ``MemoryManager.initialize_all`` first
calls ``agent.memory_caller_scope.claim_instance(provider, home)`` and then
``provider.initialize(hermes_home=home, ...)``. ``claim_instance`` only refuses a second
profile on a multiplexed gateway; in the WebUI process it records the new owner and lets
the construction for profile beta re-initialise the instance that profile alpha's CACHED
agent still uses. Alpha's next turn then reads and writes beta's bank (harness cases
order_ab, rebuild, healthy_clobber), even after P1 made every construction bind its own
profile home.

These tests reuse the P0 turn driver and the P0 shared-instance provider. The Agent
stand-in binds memory the way ``MemoryManager.initialize_all`` does (Agent 419e6627+):
it looks up ``agent.memory_manager.claim_instance`` at call time, drops the provider when
the claim is refused, and only then calls ``initialize``. One test drives the REAL
``MemoryManager`` with a real ``MemoryProvider`` subclass.

"Wrong-bank row": a row in profile H's bank written by an agent serving another profile.
"""
# ruff: noqa: F811  (pytest fixtures imported from the #8517 P0/P1 module)
from __future__ import annotations

import inspect
import logging
import sys

import pytest

from tests.test_issue8517_profile_scoped_memory import (  # noqa: F401  (fixtures)
    SENTINEL,
    FakeMemoryManager,
    SharedInstanceMemoryProvider,
    _cached_agent,
    _key,
    _profile_identity,
    bank_rows,
    hc,
    homes,
    run_turn,
)

mm_mod = pytest.importorskip("agent.memory_manager")
if not callable(getattr(mm_mod, "claim_instance", None)):  # pragma: no cover - old Agent
    pytest.skip("installed Agent predates claim_instance", allow_module_level=True)


def _original_claim():
    fn = mm_mod.claim_instance
    while getattr(fn, "__wrapped__", None) is not None:
        fn = fn.__wrapped__
    return fn


@pytest.fixture
def guard_state(monkeypatch):
    """Every test starts with the Agent's own ``claim_instance`` and an empty WebUI
    ownership registry; both are restored afterwards."""
    monkeypatch.setattr(mm_mod, "claim_instance", _original_claim())
    import api.memory_profile_scope as scope  # noqa: PLC0415

    monkeypatch.setattr(scope, "_logged_once", set())
    if hasattr(scope, "_instance_owners"):
        monkeypatch.setattr(scope, "_instance_owners", {})
    return scope


def make_claiming_agent_cls(provider_for_build):
    """Stand-in AIAgent whose memory binding follows ``MemoryManager.initialize_all``:
    ambient ``get_hermes_home()`` -> ``agent.memory_manager.claim_instance`` (looked up
    per call, like the real module global) -> drop on refusal, else ``initialize``."""

    class ClaimingAgent:
        built: list = []

        def __init__(self, session_id=None, skip_memory=False, **kwargs):
            self.session_id = session_id
            self.skip_memory = skip_memory
            self._memory_manager = None
            self._provider = None
            if not skip_memory:
                provider = provider_for_build()
                home = str(sys.modules["hermes_constants"].get_hermes_home())
                identity = _profile_identity(home)
                if mm_mod.claim_instance(provider, home):
                    provider.initialize(session_id, hermes_home=home, agent_identity=identity)
                    self._memory_manager = FakeMemoryManager(provider, home, identity)
                    self._provider = provider
            ClaimingAgent.built.append(self)

        def run_conversation(self, *args, **kwargs):
            self.turn(kwargs.get("user_message"))
            raise RuntimeError(SENTINEL)

        def turn(self, msg):
            """One turn's memory write (tool call / autosave) through the agent's provider."""
            if self._memory_manager is not None:
                self._provider.remember(f"{self.session_id}:{msg}")

    return ClaimingAgent


def _writer_profile(row) -> str:
    # session ids from run_turn: issue8517-<profile|root>-<hex>
    prof = str(row["marker"]).split(":", 1)[0].split("-")[1]
    return "default" if prof == "root" else prof


def wrong_bank_rows(homes) -> list:
    out = []
    for prof in ("default", "alpha", "beta"):
        out += [(prof, r) for r in bank_rows(homes[prof]) if _writer_profile(r) != prof]
    return out


def _msgs(home) -> list:
    """Turn messages in ``home``'s bank (the streaming path prefixes a workspace line)."""
    return [str(r["marker"]).split(":", 1)[1].splitlines()[-1] for r in bank_rows(home)]


def _shared_turns(run_turn, cls, profile, msg="t"):
    sid, _ = run_turn(profile=profile, agent_cls=cls, msg=msg)
    return sid


def _cached_turn(session_id, msg):
    agent = _cached_agent(session_id)
    assert agent is not None, f"session {session_id} was not cached"
    agent.turn(msg)
    return agent


# ── RED on the P1 head: the shared instance follows the latest construction ───

def test_order_ab_shape_shared_instance_never_writes_into_another_profiles_bank(
    hc, homes, run_turn, guard_state
):
    provider = SharedInstanceMemoryProvider(homes["default"])
    cls = make_claiming_agent_cls(lambda: provider)

    sid_a = _shared_turns(run_turn, cls, "alpha", "alpha-t1")
    sid_b = _shared_turns(run_turn, cls, "beta", "beta-t2")
    _cached_turn(sid_a, "alpha-t3-cached")
    _shared_turns(run_turn, cls, None, "root-t4")
    _cached_turn(sid_b, "beta-t5-cached")

    assert wrong_bank_rows(homes) == []
    assert all(_key(i["home"]) == _key(homes["alpha"]) for i in provider.inits), provider.inits
    assert _msgs(homes["alpha"]) == ["alpha-t1", "alpha-t3-cached"]


def test_rebuild_shape_owner_rebuild_never_hands_its_bank_to_the_refused_profile(
    hc, homes, run_turn, guard_state
):
    provider = SharedInstanceMemoryProvider(homes["default"])
    cls = make_claiming_agent_cls(lambda: provider)

    _shared_turns(run_turn, cls, "alpha", "alpha-t1")
    sid_b = _shared_turns(run_turn, cls, "beta", "beta-t2")
    _shared_turns(run_turn, cls, "alpha", "alpha-t3-rebuilt")  # second alpha manager
    _cached_turn(sid_b, "beta-t4-cached-after-A-rebuild")

    assert wrong_bank_rows(homes) == []
    assert not bank_rows(homes["beta"])
    assert all(_key(i["home"]) == _key(homes["alpha"]) for i in provider.inits), provider.inits


def test_refused_profile_runs_without_the_provider_and_is_warned_once(
    hc, homes, run_turn, guard_state, caplog
):
    provider = SharedInstanceMemoryProvider(homes["default"])
    cls = make_claiming_agent_cls(lambda: provider)
    caplog.set_level(logging.WARNING)

    _shared_turns(run_turn, cls, "alpha", "alpha-t1")
    sid_b1 = _shared_turns(run_turn, cls, "beta", "beta-t2")
    sid_b2 = _shared_turns(run_turn, cls, "beta", "beta-t3-second-session")

    for sid in (sid_b1, sid_b2):
        agent = _cached_agent(sid)
        assert agent is not None and agent._memory_manager is None
    shared = [r for r in caplog.records if "#8517" in r.getMessage() and "shared" in r.getMessage().lower()]
    assert len(shared) == 1, [r.getMessage() for r in shared]
    msg = shared[0].getMessage()
    assert "sbxmem" in msg and str(homes["beta"]) in msg and _key(homes["alpha"]) in msg


def test_real_memory_manager_refuses_a_shared_instance_for_a_second_profile(hc, homes, guard_state):
    from agent.memory_provider import MemoryProvider  # noqa: PLC0415
    from api.agent_runtime import require_ai_agent_class  # noqa: PLC0415

    class SharedSbx(SharedInstanceMemoryProvider, MemoryProvider):
        name = "sbxmem"

        def is_available(self):
            return True

        def get_tool_schemas(self):
            return []

    require_ai_agent_class()  # every WebUI construction site resolves the Agent class here
    singleton = SharedSbx(homes["default"])
    alpha = mm_mod.MemoryManager()
    alpha.add_provider(singleton)
    assert alpha.initialize_for_agent(session_id="s-alpha", hermes_home=str(homes["alpha"]),
                                      agent_identity="alpha")
    beta = mm_mod.MemoryManager()
    beta.add_provider(singleton)
    warnings: list = []
    assert not beta.initialize_for_agent(say=warnings.append, session_id="s-beta",
                                         hermes_home=str(homes["beta"]), agent_identity="beta")
    assert singleton.home == str(homes["alpha"]) and [i["home"] for i in singleton.inits] == [str(homes["alpha"])]
    assert not [p for p in beta.providers if p.name != "builtin"]
    assert warnings and "disabled for this profile" in warnings[0]


# ── GREEN before and after: no behaviour change for these users ─────────────

def test_single_profile_root_keeps_the_shared_provider_for_every_session(
    hc, homes, run_turn, guard_state, caplog
):
    provider = SharedInstanceMemoryProvider(homes["default"])
    cls = make_claiming_agent_cls(lambda: provider)
    caplog.set_level(logging.WARNING)

    sid_1 = _shared_turns(run_turn, cls, None, "root-t1")
    sid_2 = _shared_turns(run_turn, cls, None, "root-t2")
    _cached_turn(sid_1, "root-t3-cached")
    _cached_turn(sid_2, "root-t4-cached")

    assert _msgs(homes["default"]) == ["root-t1", "root-t2", "root-t3-cached", "root-t4-cached"]
    assert all(_cached_agent(s)._memory_manager is not None for s in (sid_1, sid_2))
    assert not [r for r in caplog.records if "#8517" in r.getMessage()]


def test_per_instance_provider_keeps_memory_for_every_profile(hc, homes, run_turn, guard_state, caplog):
    made: list = []

    def fresh():
        made.append(SharedInstanceMemoryProvider(homes["default"]))
        return made[-1]

    cls = make_claiming_agent_cls(fresh)
    caplog.set_level(logging.WARNING)

    sid_a = _shared_turns(run_turn, cls, "alpha", "alpha-t1")
    sid_b = _shared_turns(run_turn, cls, "beta", "beta-t2")
    sid_r = _shared_turns(run_turn, cls, None, "root-t3")
    _cached_turn(sid_a, "alpha-t4-cached")
    _cached_turn(sid_b, "beta-t5-cached")

    assert wrong_bank_rows(homes) == []
    assert len(bank_rows(homes["alpha"])) == 2 and len(bank_rows(homes["beta"])) == 2
    assert len(bank_rows(homes["default"])) == 1
    assert all(_cached_agent(s)._memory_manager is not None for s in (sid_a, sid_b, sid_r))
    assert not [r for r in caplog.records if "#8517" in r.getMessage()]


# ── the guard's own contract ────────────────────────────────────────────────

class _Prov:
    name = "sbxmem"


class _SlotProv:  # no __weakref__: cannot be weakly referenced
    __slots__ = ()
    name = "slotmem"


def test_concurrent_claims_for_two_profiles_leave_exactly_one_owner(hc, homes, guard_state):
    import threading  # noqa: PLC0415

    assert guard_state.install_shared_provider_instance_guard()
    provider = _Prov()
    barrier = threading.Barrier(8)
    results: dict = {}

    def claim(i):
        home = homes["alpha"] if i % 2 else homes["beta"]
        barrier.wait(5)
        results[i] = (home, mm_mod.claim_instance(provider, str(home)))

    threads = [threading.Thread(target=claim, args=(i,)) for i in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(10)
    winners = {_key(h) for h, ok in results.values() if ok}
    assert len(results) == 8 and len(winners) == 1, results


def test_a_collected_instance_releases_its_owner(hc, homes, guard_state):
    import gc  # noqa: PLC0415

    assert guard_state.install_shared_provider_instance_guard()
    first = _Prov()
    assert mm_mod.claim_instance(first, str(homes["alpha"]))
    assert not mm_mod.claim_instance(first, str(homes["beta"]))
    del first
    gc.collect()
    second = _Prov()  # may reuse the id: a dead entry must not refuse it
    assert mm_mod.claim_instance(second, str(homes["beta"]))


def test_the_agents_own_refusal_is_kept_and_not_recorded_as_owner(hc, homes, guard_state, monkeypatch):
    calls: list = []

    def agent_claim(provider, home):
        calls.append(str(home))
        return str(home) != str(homes["alpha"])  # e.g. a multiplexed refusal

    monkeypatch.setattr(mm_mod, "claim_instance", agent_claim)
    assert guard_state.install_shared_provider_instance_guard()
    provider = _Prov()
    assert not mm_mod.claim_instance(provider, str(homes["alpha"]))
    assert mm_mod.claim_instance(provider, str(homes["beta"]))  # alpha never became the owner
    assert not mm_mod.claim_instance(provider, str(homes["alpha"]))
    assert calls == [str(homes["alpha"]), str(homes["beta"])]  # cross-home refusal is WebUI's


def test_an_instance_without_weak_reference_support_is_still_tracked(hc, homes, guard_state):
    assert guard_state.install_shared_provider_instance_guard()
    provider = _SlotProv()
    assert mm_mod.claim_instance(provider, str(homes["alpha"]))
    assert mm_mod.claim_instance(provider, str(homes["alpha"]))
    assert not mm_mod.claim_instance(provider, str(homes["beta"]))


def test_install_is_idempotent_and_reinstalls_on_a_reloaded_module(hc, homes, guard_state, monkeypatch):
    original = mm_mod.claim_instance
    assert guard_state.install_shared_provider_instance_guard()
    guarded = mm_mod.claim_instance
    assert guarded is not original and guarded.__wrapped__ is original
    assert guard_state.install_shared_provider_instance_guard()
    assert mm_mod.claim_instance is guarded  # never wrapped twice
    monkeypatch.setattr(mm_mod, "claim_instance", original)  # a reloaded Agent module
    assert guard_state.install_shared_provider_instance_guard()
    assert mm_mod.claim_instance is not original and mm_mod.claim_instance.__wrapped__ is original


def test_agent_without_the_claim_seam_is_left_alone_and_logged_once(guard_state, monkeypatch, caplog):
    import types  # noqa: PLC0415

    old_agent = types.ModuleType("agent.memory_manager")
    monkeypatch.setitem(sys.modules, "agent.memory_manager", old_agent)
    caplog.set_level(logging.WARNING)
    assert not guard_state.install_shared_provider_instance_guard()
    assert not guard_state.install_shared_provider_instance_guard()
    assert not hasattr(old_agent, "claim_instance")
    assert len([r for r in caplog.records if "no memory provider instance claim" in r.getMessage()]) == 1


def test_installed_agent_claims_before_it_initialises():
    """The seam the guard relies on: initialize_all looks claim_instance up as a module
    global and drops a refused provider before calling initialize."""
    src = inspect.getsource(mm_mod.MemoryManager.initialize_all)
    assert "claim_instance" in mm_mod.MemoryManager.initialize_all.__code__.co_names
    assert src.index("claim_instance(") < src.index(".initialize(")
    assert "_drop_provider" in src
