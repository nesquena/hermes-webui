"""A declined async-delegation completion is handed back to the Agent before the retry sweep.

Current Agent cores offer each restored completion once per process (``tools.async_delegation._offered``) and expect
a consumer that discards its in-memory copy to call ``return_completion_offer(evt)``. WebUI's claim-retry path
discarded the copy without handing it back, so the restore sweep skipped the still-pending row until WebUI restarted.
These tests use a fake ``tools.async_delegation`` so they run without an installed Agent (CI installs none).
"""

from __future__ import annotations

import sys
import types

import pytest

import api.process_event_utils as peu

EVT = {"type": "async_delegation", "delegation_id": "deleg-123", "summary": "done"}


def _install_fake_agent(monkeypatch, *, durable_state="pending", with_return=True, return_raises=False):
    calls = {"returned": [], "armed": []}
    mod = types.ModuleType("tools.async_delegation")

    def get_durable_delegation(delegation_id):
        return {"delegation_id": delegation_id, "delivery_state": durable_state}

    mod.get_durable_delegation = get_durable_delegation
    if with_return:
        def return_completion_offer(evt):
            if return_raises:
                raise RuntimeError("boom")
            calls["returned"].append(evt.get("delegation_id"))
        mod.return_completion_offer = return_completion_offer

    pkg = types.ModuleType("tools")
    pkg.async_delegation = mod
    monkeypatch.setitem(sys.modules, "tools", pkg)
    monkeypatch.setitem(sys.modules, "tools.async_delegation", mod)
    monkeypatch.setattr(
        peu, "_arm_async_delegation_restore_sweep",
        lambda queue, delay: calls["armed"].append(delay) or True,
    )
    return calls


def test_pending_completion_offer_is_handed_back_before_the_sweep(monkeypatch):
    calls = _install_fake_agent(monkeypatch)
    assert peu.schedule_async_delegation_claim_retry(dict(EVT), object(), delay=0.5) is True
    assert calls["returned"] == ["deleg-123"]
    assert calls["armed"] == [0.5]


def test_older_agent_without_return_completion_offer_still_arms_the_sweep(monkeypatch):
    calls = _install_fake_agent(monkeypatch, with_return=False)
    assert peu.schedule_async_delegation_claim_retry(dict(EVT), object(), delay=0.5) is True
    assert calls["returned"] == []
    assert calls["armed"] == [0.5]


def test_non_pending_row_is_not_handed_back(monkeypatch):
    calls = _install_fake_agent(monkeypatch, durable_state="delivered")
    assert peu.schedule_async_delegation_claim_retry(dict(EVT), object(), delay=0.5) is False
    assert calls["returned"] == []
    assert calls["armed"] == []


def test_a_failing_hand_back_does_not_block_the_retry(monkeypatch):
    calls = _install_fake_agent(monkeypatch, return_raises=True)
    assert peu.schedule_async_delegation_claim_retry(dict(EVT), object(), delay=0.5) is True
    assert calls["armed"] == [0.5]


@pytest.mark.parametrize("evt", [None, {}, {"type": "process_exit"}, {"type": "async_delegation"}])
def test_non_delegation_events_are_ignored(monkeypatch, evt):
    calls = _install_fake_agent(monkeypatch)
    assert peu.schedule_async_delegation_claim_retry(evt, object(), delay=0.5) is False
    assert calls["returned"] == [] and calls["armed"] == []
