"""Regression: a cached agent's borrowed persistence handle must survive SessionDB reuse.

hermes-agent 0.23.2 turned ``hermes_state.SessionDB`` into a shim over
``persistence_assembly`` handles. The shim has no ``_conn`` (the only thing the
WebUI probed), and ``agent_init_session.stand_in_interface`` hands the agent the
``Persistence`` handle the SessionDB held **at build time**, which the agent
writes through for the rest of its life. Probing ``_conn`` therefore reported a
live handle as closed, ``_adopt_session_db_for_cached_agent`` closed it while
re-pointing ``agent._session_db``, and the next turn died with
``RuntimeError: persistence handle used after close``.
"""

from __future__ import annotations

import sys
import types
from pathlib import Path

import pytest


REPO = Path(__file__).resolve().parent.parent
if str(REPO) not in sys.path:
    sys.path.insert(0, str(REPO))


class FakeStore:
    """A storage-interface handle: unusable once closed, like ``Persistence``."""

    def __init__(self) -> None:
        self.closed = False

    def touch(self) -> str:
        if self.closed:
            raise RuntimeError("persistence handle used after close")
        return "ok"


class LegacySessionDB:
    """``hermes_state.SessionDB`` before 0.23.2: an open sqlite connection."""

    def __init__(self) -> None:
        self._conn = object()
        self.close_calls = 0

    def close(self) -> None:
        self._conn = None
        self.close_calls += 1


class ShimSessionDB:
    """``hermes_state.SessionDB`` on 0.23.2+: a handle over persistence_assembly."""

    def __init__(self) -> None:
        self._store = FakeStore()
        self._closed = False
        self.close_calls = 0

    def close(self) -> None:
        if not self._closed:
            self._store.closed = True
        self._closed = True
        self.close_calls += 1


def _agent(db, *, owns=False):
    """The shape ``agent_init_session`` leaves behind for a caller-supplied SessionDB."""
    return types.SimpleNamespace(
        _session_db=db,
        persistence=db._store if hasattr(db, "_store") else None,
        _owns_session_db=owns,
    )


@pytest.mark.parametrize("db_factory", [LegacySessionDB, ShimSessionDB])
def test_probe_reports_a_live_handle_as_open(db_factory):
    from api import streaming

    db = db_factory()
    assert streaming._session_db_is_open(db) is True
    db.close()
    assert streaming._session_db_is_open(db) is False


def test_probe_still_rejects_missing_and_unbound_handles():
    from api import streaming

    assert streaming._session_db_is_open(None) is False
    # A shim whose store was released without being marked closed.
    unbound = types.SimpleNamespace(_store=None, _closed=False)
    assert streaming._session_db_is_open(unbound) is False


def test_open_handle_is_kept_and_the_borrowed_handle_stays_usable():
    from api import streaming

    existing = ShimSessionDB()
    agent = _agent(existing)
    borrowed = agent.persistence
    fresh = ShimSessionDB()

    kept = streaming._adopt_session_db_for_cached_agent(agent, fresh)

    assert kept is existing
    assert existing.close_calls == 0, "an open SessionDB must not be closed under a live agent"
    assert fresh.close_calls == 1, "the unused per-stream handle is the one to close"
    assert agent._session_db is existing
    assert agent.persistence is borrowed
    assert borrowed.touch() == "ok", "the agent must still be able to write"


def test_dead_handle_is_replaced_and_the_agent_rebound():
    from api import streaming

    existing = ShimSessionDB()
    agent = _agent(existing)
    existing.close()  # the state a poisoned cached agent is in
    fresh = ShimSessionDB()

    chosen = streaming._adopt_session_db_for_cached_agent(agent, fresh)

    assert chosen is fresh
    assert agent._session_db is fresh
    assert agent.persistence is fresh._store
    assert agent.persistence.touch() == "ok"


def test_agent_that_owns_its_store_is_not_rebound():
    from api import streaming

    own = ShimSessionDB()
    agent = types.SimpleNamespace(_session_db=own, persistence=own._store, _owns_session_db=True)
    original = agent.persistence
    sibling = ShimSessionDB()

    streaming._rebind_agent_persistence(agent, sibling)

    assert agent.persistence is original, "an agent-owned handle is released by the agent, never re-pointed"