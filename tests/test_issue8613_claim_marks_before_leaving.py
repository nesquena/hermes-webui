"""#8613: the last-holder claim marks its entries as claiming BEFORE the agent leaves
the holder sets.

Leaving allocates (the queued-work check lists the manager's futures); an allocation
can run a GC on the claiming thread, and a collected agent's weak-reference callback
then runs inline, because the holder lock is reentrant. Its prune deletes any entry with
no holders, no teardown, no queued work, no claim and no intent. Marked first, the entry
survives; unmarked, the proof answers "not tracked" and the release neither runs nor is
retained, which is the leak this fix exists to close.
"""
import weakref

import api.memory_profile_scope as mps


class _Provider:
    name = "mem"


class _Manager:
    _background_futures = {}


class _Agent:
    def __init__(self):
        self.memory_manager = _Manager()


def _hold(agent, provider):
    entry = mps._provider_holders.get(id(provider))
    if entry is None:
        entry = mps._InstanceHolders(weakref.ref(provider), "home")
        mps._provider_holders[id(provider)] = entry
    # the callback-bearing reference production registers, so a collection fires the real callback
    entry.holders[id(agent)] = mps._AgentHolder(mps._weak_agent_ref(agent), mps._weak_manager_ref(agent.memory_manager))
    return entry


def test_a_collection_while_the_agent_leaves_cannot_prune_the_claimed_entry(monkeypatch):
    monkeypatch.setattr(mps, "_provider_holders", {})
    monkeypatch.setattr(mps, "_pending_forgets", [])
    monkeypatch.setattr(mps, "_agent_manager", lambda a: a.memory_manager)
    provider = _Provider()
    agent = _Agent()
    others = [_Agent()]  # an unrelated co-holder, about to be collected
    entry = _hold(agent, provider)
    _hold(others[0], provider)
    monkeypatch.setattr(mps, "_agent_holds_instances_locked", lambda a: (True, "", [provider]))
    real_outstanding = mps._manager_has_outstanding_work
    collected = []

    def outstanding_work_with_a_collection(manager):
        # The allocation point inside the claim's holder removal: drop the last
        # reference to the unrelated agent here, so its real weak-reference
        # callback fires on this thread, under the held (reentrant) lock.
        if not collected:
            collected.append(True)
            others.clear()
        return real_outstanding(manager)

    monkeypatch.setattr(mps, "_manager_has_outstanding_work", outstanding_work_with_a_collection)

    ok, why, claimed = mps._claim_last_held_teardown(agent)

    assert collected, "the collection never happened at the allocation point"
    assert mps._provider_holders.get(id(provider)) is entry, f"entry pruned mid-claim: {why!r}"
    assert (ok, claimed) == (True, [provider]), why
    assert entry.teardown and entry.claiming == 0


def test_a_collection_while_a_departed_agent_files_its_hold_cannot_prune_the_entry(monkeypatch):
    """The sibling site: a collected agent's forget files its manager's queued-work
    hold after dropping the holder; a nested collection at that allocation must not
    take the now holder-less entry before the hold is filed."""
    monkeypatch.setattr(mps, "_provider_holders", {})
    monkeypatch.setattr(mps, "_pending_forgets", [])
    provider = _Provider()
    departed = [_Agent()]
    bystander = [_Agent()]
    entry = _hold(departed[0], provider)
    _hold(bystander[0], provider)
    departed_manager = departed[0].memory_manager
    departed_manager._background_futures = {"save": object()}  # queued work keeps the hold
    departed_pid = id(departed[0])
    real_outstanding = mps._manager_has_outstanding_work
    collected = []

    def outstanding_work_with_a_collection(manager):
        if manager is departed_manager and not collected:
            collected.append(True)
            bystander.clear()  # the unrelated co-holder is collected right here
        return real_outstanding(manager)

    monkeypatch.setattr(mps, "_manager_has_outstanding_work", outstanding_work_with_a_collection)
    monkeypatch.setattr(mps, "_future_is_done", lambda future: False)

    with mps._provider_holders_lock:
        departed.clear()  # its own callback fires inline: the forget under test
    assert collected, "the nested collection never happened at the allocation point"
    assert mps._provider_holders.get(id(provider)) is entry, "entry pruned while the hold was being filed"
    assert id(departed_manager) in entry.work and entry.claiming == 0
    assert departed_pid not in entry.holders
