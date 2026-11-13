"""#8810: a pressure shed must not close a memory provider a live agent still uses.

Native ``AIAgent.release_clients()`` (Agent 2026-10-19+) ends with
``MemoryManager.release_soft()``, which runs ``shutdown_all()`` on every provider
instance the manager serves.  The pressure valve releases an agent's clients on
the way to dropping its transcript, so with a provider that hands one instance
to every agent of a profile (mnemosyne-hermes 0.7.1) the shed closed the client
under a peer that was still cached: the peer's next save failed with
``memory provider client is closed``.

The release is therefore split along the #8613 instance-lifetime protocol
(``api.memory_profile_scope.release_shed_agent_clients``):

* LLM clients always go;
* the provider is torn down only when this agent is its LAST holder, claimed
  atomically with the proof and run in the provider's own profile home;
* a construction acquiring the instance, or queued memory work, keeps it alive —
  and a refusal that is only queued work retains the release until that work
  drains (no third construction may be needed).

These tests drive the REAL ``MemoryManager`` (``release_soft`` ->
``claim_teardown`` -> bounded flush -> ``shutdown_all``) and the REAL pressure
release entry point, with the shared-instance provider from the #8613 suite.  The
oracles are the provider's own state (``shutdowns``, ``client.is_closed``), the
surviving agent's NEXT save, and — for the shared case — that the native
``release_clients()`` never ran at all.
"""
# ruff: noqa: F811  (pytest fixtures imported from the #8517 P0 module)
from __future__ import annotations

import threading
import time

import pytest

from tests.test_issue8517_profile_scoped_memory import (  # noqa: F401 (fixtures)
    hc,
    homes,
)
from tests.test_issue8613_provider_instance_holders import (  # noqa: F401
    SharedInstanceProvider,
    _build,
    _pause_saves,
    _queue_paused_save,
    _shared_agent,
    holder_registry,
    make_holder_agent_cls,
)

import api.agent_cache_governance as governance
import api.memory_profile_scope as scope

mm_mod = pytest.importorskip("agent.memory_manager")
if not callable(getattr(getattr(mm_mod, "MemoryManager", None), "release_soft", None)):
    pytest.skip("installed Agent predates MemoryManager.release_soft", allow_module_level=True)


def _phased_agent_cls(provider_for_build, *, home, **kwargs):
    """The #8613 stand-in Agent plus the LLM-client phases of the native
    ``release_clients()``, so a release that must stop before the memory part can
    be observed: ``llm_clients`` counts the phases, and ``release_clients`` is
    the native-call spy the shared case asserts never ran.
    """
    base = make_holder_agent_cls(provider_for_build, home=home, **kwargs)

    class PhasedAgent(base):
        def __init__(self, *args, **kw):
            super().__init__(*args, **kw)
            self.llm_clients = 0
            self.native_releases = 0

        # The phases the native method runs before its memory release.
        def _drop_shared_client(self, close_fn):
            self.llm_clients += 1

        def _retire_shared_openai_client(self, client, *, reason=""):
            return None

        def _close_request_clients(self, reason=""):
            self.llm_clients += 1

        def _close_codex_session(self):
            return None

        def release_clients(self):
            # Native contract under test: LLM clients AND the memory provider.
            self.native_releases += 1
            self.llm_clients += 1
            if self._memory_manager is not None:
                self._memory_manager.release_soft()

    return PhasedAgent


def _build_phased(home, provider_for_build, session_id):
    agent, cacheable = scope.construct_profile_scoped_agent(
        _phased_agent_cls(provider_for_build, home=home),
        {"model": "m"},
        profile_home=str(home),
        session_id=session_id,
    )
    assert cacheable is True
    return agent


def _wait_for(predicate, timeout: float) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(0.02)
    return bool(predicate())


def _released_exactly_once(provider, timeout: float = 10.0) -> None:
    assert _wait_for(lambda: provider.shutdowns >= 1, timeout), (
        "the provider was never released after the queued work drained: the retained "
        "release was dropped, and only another agent's boundary could release it"
    )
    time.sleep(0.2)  # a second (double) release would land here
    assert provider.shutdowns == 1, f"released {provider.shutdowns} times"
    assert provider.client.is_closed is True


# ── (a) a live peer keeps the provider ──────────────────────────────────────

def test_a_shed_agent_never_closes_the_provider_a_live_peer_still_holds(
    hc, homes, holder_registry
):
    """Two agents of one profile share the instance. Shedding the first must not
    run the native release at all: its memory half would shut the instance down
    under the peer, whose next save goes through that very client."""
    provider = SharedInstanceProvider()
    first = _build_phased(homes["alpha"], lambda: provider, "8810-peer-a")
    peer = _build_phased(homes["alpha"], lambda: provider, "8810-peer-b")

    governance.soft_release_transcript(first, session_id="8810-peer-a")

    assert first._session_messages == [], "the shed transcript was kept"
    assert first.native_releases == 0, (
        "the pressure shed called release_clients() while a live peer held the instance"
    )
    assert first.llm_clients >= 1, "the shed agent kept its LLM clients"
    assert provider.shutdowns == 0, "a shared provider was torn down under a live agent"
    assert provider.client.is_closed is False

    # ORACLE: the peer's next save lands through the shared client.
    peer.save("the peer still owns the provider")
    assert provider.writes == ["the peer still owns the provider"]


def test_the_last_holder_is_released_and_the_instance_is_claimable_again(
    hc, homes, holder_registry
):
    """The sole holder may go: the provider is torn down with the claim, and the
    next construction re-initialises the instance exactly as after a session-end
    teardown."""
    provider = SharedInstanceProvider()
    agent = _build_phased(homes["alpha"], lambda: provider, "8810-sole")
    assert scope.agent_is_last_provider_holder(agent)[0] is True

    governance.soft_release_transcript(agent, session_id="8810-sole")

    assert agent.native_releases == 1, "the last holder's release never ran"
    assert provider.shutdowns == 1
    assert provider.client.is_closed is True

    rebuilt = _build_phased(homes["alpha"], lambda: provider, "8810-sole-again")
    rebuilt.save("after re-acquisition")
    assert provider.writes == ["after re-acquisition"]


# ── (b) a construction acquiring the instance blocks the teardown ───────────

def test_a_construction_acquiring_the_instance_keeps_it_alive_through_a_shed(
    hc, homes, holder_registry
):
    """The new constructor has finished its native initialisation but has not
    registered yet. The shed must wait for it (bounded) and then leave the
    instance up: a teardown here closes the client the new agent just acquired."""
    provider = SharedInstanceProvider()
    old = _build_phased(homes["alpha"], lambda: provider, "8810-acq-old")

    initialised = threading.Event()
    proceed = threading.Event()

    def pause_after_native_init():
        initialised.set()
        assert proceed.wait(30), "the paused construction was never resumed"

    built: list = []

    def build():
        built.append(
            _build(
                homes["alpha"],
                lambda: provider,
                "8810-acq-new",
                after_init=pause_after_native_init,
            )
        )

    builder = threading.Thread(target=build, name="8810-acquisition", daemon=True)
    builder.start()
    assert initialised.wait(30), "the constructor never initialised its native manager"

    shed = threading.Thread(
        target=lambda: governance.soft_release_transcript(old, session_id="8810-acq-old"),
        name="8810-shed",
        daemon=True,
    )
    shed.start()
    proceed.set()
    builder.join(30)
    shed.join(30)
    assert not builder.is_alive() and not shed.is_alive()

    survivor = built[0]
    assert old.native_releases == 0, (
        "the shed ran the native release while a construction was acquiring the instance"
    )
    assert provider.shutdowns == 0, "the provider was torn down under the agent being built"
    survivor.save("save after the construction won the instance")
    assert provider.writes == ["save after the construction won the instance"]


# ── (c) queued memory work retains the release until it drains ──────────────

def test_a_shed_agents_queued_save_delays_the_release_until_it_drains(
    hc, homes, holder_registry
):
    """The sole holder is shed while its own save is still writing through the
    provider. The release must be refused now (the save would lose its client)
    and must run on its own once that save has landed — no third construction."""
    provider = _pause_saves(SharedInstanceProvider())
    agent = _build_phased(homes["alpha"], lambda: provider, "8810-queued")
    _queue_paused_save(agent, provider, "the shed turn", hc)

    governance.soft_release_transcript(agent, session_id="8810-queued")

    assert provider.shutdowns == 0, "the provider was torn down under a queued save"
    assert provider.client.is_closed is False

    provider.save_resume.set()
    assert provider.save_landed.wait(30), "the queued save never completed"
    assert provider.writes == ["the shed turn"]
    _released_exactly_once(provider)


def test_an_aborted_release_that_keeps_the_handle_never_touches_the_provider(
    hc, homes, holder_registry
):
    """The aborted-release path keeps the handle because a pending extraction is
    still delivered through its provider; even as the last holder, that release
    must be clients-only."""
    provider = SharedInstanceProvider()
    agent = _build_phased(homes["alpha"], lambda: provider, "8810-abort")
    assert scope.agent_is_last_provider_holder(agent)[0] is True

    governance._release_agent_clients(
        agent, session_id="8810-abort", release_provider=False
    )

    assert agent.native_releases == 0, "a clients-only release ran the memory release"
    assert agent.llm_clients >= 1, "the LLM clients were not released"
    assert provider.shutdowns == 0
    assert provider.client.is_closed is False
    agent.save("the pending extraction can still be delivered")
    assert provider.writes == ["the pending extraction can still be delivered"]
