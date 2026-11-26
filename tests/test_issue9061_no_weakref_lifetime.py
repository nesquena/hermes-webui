"""#9061 items 1 and 3: the #8517 shared-instance guard must not keep a
non-weakref provider alive for the whole WebUI process, and that lifetime must
be proven with a REAL MemoryManager and a ``__slots__`` provider.

Item 1 (lifetime). ``_instance_ref`` falls back to a STRONG reference
(``lambda: provider``) when ``weakref.ref(provider)`` raises ``TypeError`` — a
provider without weak reference support (a ``__slots__`` provider). The guard's
own strong reference then keeps the provider alive, so the tracked entry is
never pruned: every such instance stays in memory for the life of the WebUI
process, and it never transfers to another profile after its first profile's
managers have gone.

The Agent already solved the same problem (agent c9de60702c0d,
``agent/memory_caller_scope.claim_instance``): a provider that cannot be weakly
referenced is anchored on the MEMORY MANAGER that holds it, and the claim is
released with the last manager. The guard receives that manager as the Agent's
``manager=`` argument (#9060) and keys its own entry on the same signal.

Item 3 (coverage). These tests drive the guard with the REAL
``agent.memory_manager.MemoryManager`` and a real ``__slots__`` provider, so the
coverage the #9240 stand-in callback tests cannot give is actually proven.

The Agent that owns ``claim_instance`` is optional: when it is not importable
the real-manager tests are SKIPPED with the reason (``pytest -rs`` lists them),
never faked. The guard-level tests run either way.
"""
from __future__ import annotations

import gc
import sys
import types
import weakref

import pytest


@pytest.fixture
def scope(monkeypatch):
    """Fresh guard state: no recorded owners, nothing logged once."""
    import api.memory_profile_scope as scope  # noqa: PLC0415

    monkeypatch.setattr(scope, "_logged_once", set())
    monkeypatch.setattr(scope, "_instance_owners", {})
    # The queued-forget queue only exists once the entry has a lifetime anchor of
    # its own; on an older tree the attribute is absent, and the fixture must not
    # fail there (a fixture error would hide the behaviour under test).
    if hasattr(scope, "_pending_instance_forgets"):
        monkeypatch.setattr(scope, "_pending_instance_forgets", [])
    return scope


class _SlotsProvider:
    """A provider WITHOUT weak reference support — ``weakref.ref`` raises TypeError.

    This is the shape of a duck-typed plugin provider that declares
    ``__slots__``: the Agent's own claim has to anchor it on its manager for the
    same reason (#146293, agent c9de60702c0d).
    """

    __slots__ = ("bound_home", "name")

    def __init__(self, name: str = "slots_only"):
        self.bound_home = None
        self.name = name


class _WeakrefProvider:
    """A provider that DOES support weak references (the normal case)."""

    def __init__(self, name: str = "weakref_ok"):
        self.name = name


class _Manager:
    """A stand-in for the memory manager: weakly referenceable, like the real one."""

    def __init__(self):
        self.held = []


def _install_stand_in(monkeypatch, claim=None):
    """Install a stand-in ``agent.memory_manager`` whose ``claim_instance`` is ``claim``."""
    mod = types.ModuleType("agent.memory_manager")
    if claim is None:
        def claim(provider, home, *, manager=None):
            return True
    mod.claim_instance = claim
    monkeypatch.setitem(sys.modules, "agent.memory_manager", mod)
    return mod


# ── item 1: the lifetime of a non-weakref provider ─────────────────────────


def test_a_slots_provider_cannot_be_weakly_referenced():
    """The premise of item 1: a ``__slots__`` provider has no weak reference."""
    provider = _SlotsProvider()
    with pytest.raises(TypeError):
        weakref.ref(provider)


def test_the_entry_is_released_with_the_last_manager_that_held_the_provider(scope, monkeypatch):
    """Item 1: the guard's entry dies with the manager that holds the instance.

    Before the fix the guard tracked the instance through its OWN strong
    reference, so the entry outlived every manager and lived for the whole
    WebUI process.
    """
    mod = _install_stand_in(monkeypatch)
    assert scope.install_shared_provider_instance_guard() is True

    provider = _SlotsProvider()
    manager = _Manager()
    assert mod.claim_instance(provider, "home-alpha", manager=manager) is True
    assert id(provider) in scope._instance_owners  # tracked while a manager holds it

    del manager
    gc.collect()

    assert id(provider) not in scope._instance_owners, (
        "the guard entry outlived the manager that held the instance"
    )


def test_the_entry_survives_while_another_manager_still_holds_the_provider(scope, monkeypatch):
    """Control: the entry must NOT be released while another manager holds it.

    The Agent keeps one weak reference per manager (agent c9de60702c0d keeps a
    LIST of them); releasing the entry with the first manager would hand the
    instance to a second profile while a live agent still writes through it.
    """
    mod = _install_stand_in(monkeypatch)
    assert scope.install_shared_provider_instance_guard() is True

    provider = _SlotsProvider()
    first, second = _Manager(), _Manager()
    assert mod.claim_instance(provider, "home-alpha", manager=first) is True

    # A second manager of the SAME profile claims the same instance.
    assert mod.claim_instance(provider, "home-alpha", manager=second) is True

    del first
    gc.collect()
    assert id(provider) in scope._instance_owners, (
        "the entry was released while a second manager still held the instance"
    )

    del second
    gc.collect()
    assert id(provider) not in scope._instance_owners


def test_the_entry_survives_when_the_LATER_manager_dies_first(scope, monkeypatch):
    """The other order: the manager that claimed LAST dies first.

    Every manager that holds the instance keeps its own anchor, so the entry
    must not be tied to the most recent one. Releasing it here would let a
    second profile claim an instance a live agent still writes through — the
    cross-profile memory mix the #8517 guard exists to prevent.
    """
    mod = _install_stand_in(monkeypatch)
    assert scope.install_shared_provider_instance_guard() is True

    provider = _SlotsProvider()
    first, second = _Manager(), _Manager()
    assert mod.claim_instance(provider, "home-alpha", manager=first) is True
    assert mod.claim_instance(provider, "home-alpha", manager=second) is True

    # The LAST manager dies; the first one still holds the instance.
    del second
    gc.collect()
    assert id(provider) in scope._instance_owners, (
        "the entry was released although the first manager still held the instance"
    )

    # A second profile must still be refused the instance.
    assert mod.claim_instance(provider, "home-beta", manager=_Manager()) is False


def test_the_instance_can_then_be_claimed_by_another_profile(scope, monkeypatch):
    """Item 1, the effect the issue names: the instance must become claimable
    again once its managers are gone, so a later profile can serve it."""
    mod = _install_stand_in(monkeypatch)
    assert scope.install_shared_provider_instance_guard() is True

    provider = _SlotsProvider()
    manager = _Manager()
    assert mod.claim_instance(provider, "home-alpha", manager=manager) is True

    # While the first profile's manager holds it, a second profile is refused.
    other_manager = _Manager()
    assert mod.claim_instance(provider, "home-beta", manager=other_manager) is False

    del manager
    gc.collect()

    # The instance is free now: another profile may claim it.
    assert mod.claim_instance(provider, "home-beta", manager=other_manager) is True


def test_a_provider_with_weak_reference_support_still_dies_with_the_provider(scope, monkeypatch):
    """Regression control: the normal case keeps its own lifetime.

    The provider is its own anchor, so the entry disappears when the provider
    is collected — the behaviour the guard had before this change.
    """
    mod = _install_stand_in(monkeypatch)
    assert scope.install_shared_provider_instance_guard() is True

    provider = _WeakrefProvider()
    manager = _Manager()
    assert mod.claim_instance(provider, "home-alpha", manager=manager) is True
    pid = id(provider)
    assert pid in scope._instance_owners

    del provider
    gc.collect()

    assert pid not in scope._instance_owners


def test_a_provider_without_any_manager_keeps_the_strong_reference_fallback(scope, monkeypatch):
    """Regression control: an Agent that passes no manager (before c9de60702c0d)
    leaves the guard with no lifetime signal. The strong-reference fallback stays
    so the id can never be reused while the entry is tracked.

    The behaviour is pinned, not the table's layout: the entry keeps owning the
    instance even after the caller drops every reference to it, so a second
    profile is still refused the instance.
    """
    mod = _install_stand_in(monkeypatch)
    assert scope.install_shared_provider_instance_guard() is True

    provider = _SlotsProvider()
    assert mod.claim_instance(provider, "home-alpha") is True
    pid = id(provider)
    assert pid in scope._instance_owners

    # The caller drops the provider: the guard's own strong reference keeps the
    # instance tracked, so its id can never be reused by another instance.
    del provider
    gc.collect()
    assert pid in scope._instance_owners


def test_an_entry_tracked_without_a_manager_is_anchored_when_one_arrives(scope, monkeypatch):
    """A claim that arrives without a manager is re-anchored as soon as a
    manager claims the same instance, so the entry does not live forever."""
    mod = _install_stand_in(monkeypatch)
    assert scope.install_shared_provider_instance_guard() is True

    provider = _SlotsProvider()
    assert mod.claim_instance(provider, "home-alpha") is True

    manager = _Manager()
    assert mod.claim_instance(provider, "home-alpha", manager=manager) is True

    del manager
    gc.collect()
    assert id(provider) not in scope._instance_owners


def test_the_cross_profile_refusal_still_works_for_a_slots_provider(scope, monkeypatch):
    """Regression control: the guard's own refusal is unchanged for a provider
    that cannot be weakly referenced (it must not refuse MORE or LESS)."""
    mod = _install_stand_in(monkeypatch)
    assert scope.install_shared_provider_instance_guard() is True

    provider = _SlotsProvider()
    owner_manager = _Manager()
    assert mod.claim_instance(provider, "home-alpha", manager=owner_manager) is True
    assert mod.claim_instance(provider, "home-beta", manager=_Manager()) is False
    assert scope._instance_owners[id(provider)][1] == scope.profile_home_key("home-alpha")


def test_a_queued_forget_is_drained_before_the_next_claim(scope, monkeypatch):
    """A manager that died while the table lock was busy only queued its removal.

    The GC callback never waits for the lock (it can fire inside a logging
    handler's lock), so the removal lands in ``_pending_instance_forgets`` and
    is applied by the next locked table operation. If the claim does not drain
    that queue first, the stale entry keeps the instance bound to the old
    profile and refuses the next one even though every manager that held it
    is gone. Kept below the pruning threshold so ``_prune_dead_owners`` cannot
    mask the queue.
    """
    mod = _install_stand_in(monkeypatch)
    assert scope.install_shared_provider_instance_guard() is True

    provider = _SlotsProvider()
    manager = _Manager()
    assert mod.claim_instance(provider, "home-alpha", manager=manager) is True
    pid = id(provider)
    assert pid in scope._instance_owners

    # The manager dies while this thread holds the table lock: the callback
    # takes the busy path and queues the removal.
    assert scope._instance_owners_lock.acquire(blocking=False) is True
    try:
        del manager
        gc.collect()
        assert len(scope._pending_instance_forgets) == 1, (
            "the GC callback must queue its removal while the lock is busy"
        )
    finally:
        scope._instance_owners_lock.release()

    # The next profile claims the instance; the queued removal must be applied
    # first, or the stale entry refuses it.
    next_manager = _Manager()
    assert mod.claim_instance(provider, "home-beta", manager=next_manager) is True
    assert scope._instance_owners[pid][1] == scope.profile_home_key("home-beta")


# ── item 3: the REAL MemoryManager and a real ``__slots__`` provider ────────


def _real_manager_class():
    """The real ``MemoryManager``, or None when the Agent is not importable."""
    try:
        from agent.memory_manager import MemoryManager  # noqa: PLC0415
    except Exception:
        return None
    return MemoryManager


def _real_slots_provider_class():
    """A DUCK-TYPED provider that satisfies the Agent's provider contract AND has
    no weak reference support.

    The Agent's ``MemoryManager.add_provider`` does not type-check the provider
    (it only calls the contract's methods), so a plugin may hand it a
    ``__slots__`` class that does not inherit ``MemoryProvider``. That is the
    shape that cannot be weakly referenced — and the shape the Agent's own claim
    has to anchor on a manager for (#146293, agent c9de60702c0d).
    """
    class _RealSlotsProvider:
        __slots__ = ("bound_home",)

        @property
        def name(self) -> str:
            return "real_slots"

        def is_available(self) -> bool:
            return True

        def initialize(self, session_id: str, **kwargs) -> None:
            self.bound_home = kwargs.get("hermes_home")

        def get_tool_schemas(self) -> list:
            return []

        def handle_tool_call(self, tool_name: str, args: dict, **kwargs) -> str:
            return "{}"

    return _RealSlotsProvider


def test_a_real_manager_registers_initializes_and_releases_through_the_guard(
    scope, monkeypatch, tmp_path
):
    """Item 3, the whole Agent path: ``add_provider`` + ``initialize_all``.

    The earlier shape replaced ``agent.memory_manager`` and called the claim
    directly, so it still passed when the real ``add_provider`` and
    ``initialize_all`` were made to raise instead. This one registers the
    provider with the real manager, lets ``initialize_all`` drive the claim
    through the guard, then proves the entry dies with the last manager and
    the instance transfers to another profile.
    """
    MemoryManager = _real_manager_class()
    if MemoryManager is None:
        pytest.skip("the Hermes Agent (agent.memory_manager) is not importable here")

    mm = sys.modules.get("agent.memory_manager")
    if mm is None or not callable(getattr(mm, "claim_instance", None)):
        pytest.skip("this Hermes Agent has no memory_manager.claim_instance seam")

    Provider = _real_slots_provider_class()

    assert scope.install_shared_provider_instance_guard() is True
    assert getattr(mm.claim_instance, scope._GUARD_MARK, False) is True

    provider = Provider()
    with pytest.raises(TypeError):
        weakref.ref(provider)  # the premise: no weak reference support

    manager = MemoryManager()
    home = str(tmp_path / "alpha")
    manager.add_provider(provider)
    manager.initialize_all("session-1", hermes_home=home)

    pid = id(provider)
    assert provider.bound_home == home, (
        "initialize_all never reached the provider: the test would pass without "
        "exercising the manager lifecycle"
    )
    assert pid in scope._instance_owners
    assert scope._instance_owners[pid][1] == scope.profile_home_key(home)

    del manager
    gc.collect()
    assert pid not in scope._instance_owners, (
        "the guard entry outlived the real MemoryManager that held the instance"
    )

    next_manager = _Manager()
    beta = str(tmp_path / "beta")
    assert mm.claim_instance(provider, beta, manager=next_manager) is True
    assert scope._instance_owners[pid][1] == scope.profile_home_key(beta)
