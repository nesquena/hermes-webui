"""#9060: the #8517 shared-instance guard must pass the Agent's own claim arguments through.

The guard (``install_shared_provider_instance_guard`` in api/memory_profile_scope.py)
replaces ``agent.memory_manager.claim_instance`` with a wrapper. Agent c9de60702c0d
(2026-11-13) changed that seam to ``claim_instance(provider, home, *, manager=None)``,
and ``MemoryManager.initialize_all`` now calls it with ``manager=self``. A wrapper that
accepts only ``(provider, home)`` raised ``TypeError`` there. The Agent's agent_init
catches it as "Memory provider plugin init failed", so every WebUI agent silently lost
its external memory provider.

These tests drive the real guard against a stand-in ``agent.memory_manager`` module,
so they do not depend on which Agent is installed:
* a claim that requires ``manager=`` (the new seam) gets it, unchanged;
* the old two-argument claim (Agents 419e6627 .. c9de6070) still works;
* the guard's own cross-profile refusal still runs before the Agent's claim.
"""
from __future__ import annotations

import sys
import types

import pytest


@pytest.fixture
def scope(monkeypatch):
    """Fresh guard state: no recorded owners, nothing logged once."""
    import api.memory_profile_scope as scope  # noqa: PLC0415

    monkeypatch.setattr(scope, "_logged_once", set())
    monkeypatch.setattr(scope, "_instance_owners", {})
    return scope


def _install_stand_in(monkeypatch, claim):
    """Install a stand-in ``agent.memory_manager`` whose ``claim_instance`` is ``claim``."""
    mod = types.ModuleType("agent.memory_manager")
    mod.claim_instance = claim
    monkeypatch.setitem(sys.modules, "agent.memory_manager", mod)
    return mod


class _Provider:
    name = "stand-in"


def test_a_claim_that_requires_manager_gets_it_through_the_guard(scope, monkeypatch, tmp_path):
    calls = []

    def claim_instance(provider, home, *, manager):  # the c9de6070+ seam, manager required
        calls.append((provider, home, manager))
        return True

    mod = _install_stand_in(monkeypatch, claim_instance)
    assert scope.install_shared_provider_instance_guard() is True
    assert mod.claim_instance is not claim_instance  # the guard is in place

    provider, manager = _Provider(), object()
    home = tmp_path / "alpha"
    assert mod.claim_instance(provider, str(home), manager=manager) is True
    assert calls == [(provider, str(home), manager)]


def test_the_old_two_argument_claim_still_works_through_the_guard(scope, monkeypatch, tmp_path):
    calls = []

    def claim_instance(provider, home):  # Agents 419e6627 .. before c9de6070
        calls.append((provider, home))
        return True

    mod = _install_stand_in(monkeypatch, claim_instance)
    assert scope.install_shared_provider_instance_guard() is True

    provider = _Provider()
    home = str(tmp_path / "alpha")
    assert mod.claim_instance(provider, home) is True
    assert calls == [(provider, home)]


def test_the_cross_profile_refusal_still_runs_with_manager(scope, monkeypatch, tmp_path):
    calls = []

    def claim_instance(provider, home, *, manager=None):
        calls.append((home, manager))
        return True

    mod = _install_stand_in(monkeypatch, claim_instance)
    assert scope.install_shared_provider_instance_guard() is True

    shared = _Provider()  # one instance handed to two profiles
    alpha, beta = str(tmp_path / "alpha"), str(tmp_path / "beta")
    m1, m2 = object(), object()
    assert mod.claim_instance(shared, alpha, manager=m1) is True
    assert mod.claim_instance(shared, beta, manager=m2) is False  # refused before the Agent sees it
    assert mod.claim_instance(shared, alpha, manager=m2) is True  # its owner may claim again
    assert calls == [(alpha, m1), (alpha, m2)]
