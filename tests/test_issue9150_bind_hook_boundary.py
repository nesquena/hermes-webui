"""Cached-agent SessionDB adoption calls the compressor rebind hook the way the
Agent's own producers do (#9150): first two arguments positional, ``persistence``
only when the hook accepts it, and a failing hook never aborts the cached turn.
"""

import logging
from types import SimpleNamespace

import pytest

from api import streaming

try:
    from hermes_state import SessionDB
except Exception:  # pragma: no cover - depends on the selected Agent tree
    SessionDB = None

needs_session_db = pytest.mark.skipif(
    SessionDB is None,
    reason="hermes_state.SessionDB is not importable from the selected Agent tree",
)

SESSION_ID = "sid-9150-hook"


class LegacyHandle:
    """Legacy-shaped handle: open while ``_conn`` is set."""

    def __init__(self):
        self._conn = object()
        self.close_calls = 0

    def close(self):
        self.close_calls += 1
        self._conn = None


class ShimHandle:
    """Newer shim-shaped handle: owns a store in ``_store`` until close."""

    def __init__(self):
        self._closed = False
        self._store = object()
        self.close_calls = 0

    def close(self):
        self.close_calls += 1
        self._closed = True
        self._store = None


class _Recorder:
    def __init__(self):
        self.calls = []


class KeywordHook(_Recorder):
    def bind_session_state(self, session_db, session_id):
        self.calls.append((session_db, session_id, {}))


class PersistenceHook(_Recorder):
    def bind_session_state(self, session_db=None, session_id="", *, persistence=None):
        self.calls.append((session_db, session_id, {"persistence": persistence}))


class PositionalOnlyHook(_Recorder):
    def bind_session_state(self, session_db, session_id, /):
        self.calls.append((session_db, session_id, {}))


class PositionalOnlyPersistenceHook(_Recorder):
    def bind_session_state(self, session_db, session_id, /, persistence=None):
        self.calls.append((session_db, session_id, {"persistence": persistence}))


class RaisingHook(_Recorder):
    def __init__(self):
        super().__init__()
        self.fail = True

    def bind_session_state(self, session_db, session_id):
        self.calls.append((session_db, session_id, {}))
        if self.fail:
            raise RuntimeError("controlled hook failure")


def _agent(existing, hook, **extra):
    return SimpleNamespace(
        _session_db=existing, session_id=SESSION_ID, context_compressor=hook, **extra
    )


def _closed_legacy():
    handle = LegacyHandle()
    handle.close()
    return handle


@pytest.mark.parametrize("hook_type", [PositionalOnlyHook, PositionalOnlyPersistenceHook])
def test_positional_only_hook_receives_new_handle(hook_type):
    hook = hook_type()
    old, fresh = _closed_legacy(), LegacyHandle()
    agent = _agent(old, hook)

    assert streaming._adopt_session_db_for_cached_agent(agent, fresh) is fresh

    assert agent._session_db is fresh
    assert fresh.close_calls == 0
    assert [call[:2] for call in hook.calls] == [(fresh, SESSION_ID)]


def test_raising_hook_does_not_abort_adoption(caplog):
    caplog.set_level(logging.DEBUG, logger=streaming.logger.name)
    hook = RaisingHook()
    old, fresh = ShimHandle(), ShimHandle()
    old.close()
    agent = _agent(old, hook, persistence=None)

    assert streaming._adopt_session_db_for_cached_agent(agent, fresh) is fresh

    assert hook.calls == [(fresh, SESSION_ID, {})]
    assert agent._session_db is fresh
    assert agent.persistence is fresh._store
    assert streaming._session_db_is_open(fresh)
    assert fresh.close_calls == 0
    assert any(
        "bind_session_state" in record.getMessage() and record.exc_info
        for record in caplog.records
        if record.levelno == logging.DEBUG
    )


def test_keyword_hook_still_receives_persistence():
    hook = PersistenceHook()
    old, fresh = ShimHandle(), ShimHandle()
    old.close()
    agent = _agent(old, hook, persistence=None)

    streaming._adopt_session_db_for_cached_agent(agent, fresh)

    assert hook.calls == [(fresh, SESSION_ID, {"persistence": fresh._store})]
    assert agent.persistence is fresh._store


def test_keep_live_existing_handle_skips_hook():
    hook = KeywordHook()
    live, unused = LegacyHandle(), LegacyHandle()
    agent = _agent(live, hook)

    assert streaming._adopt_session_db_for_cached_agent(agent, unused) is live

    assert agent._session_db is live
    assert live.close_calls == 0
    assert unused.close_calls == 1
    assert hook.calls == []


def test_later_adoption_recovers_after_hook_failure():
    hook = RaisingHook()
    old, first, second = ShimHandle(), ShimHandle(), ShimHandle()
    old.close()
    agent = _agent(old, hook, persistence=None)

    streaming._adopt_session_db_for_cached_agent(agent, first)
    assert agent._session_db is first and agent.persistence is first._store
    assert streaming._session_db_is_open(first)

    hook.fail = False
    first.close()
    assert streaming._adopt_session_db_for_cached_agent(agent, second) is second

    assert hook.calls[-1] == (second, SESSION_ID, {})
    assert agent._session_db is second and agent.persistence is second._store
    assert streaming._session_db_is_open(second)


def test_hook_without_persistence_parameter_never_receives_it():
    hook = KeywordHook()
    old, fresh = ShimHandle(), ShimHandle()
    old.close()
    agent = _agent(old, hook, persistence=None)

    streaming._adopt_session_db_for_cached_agent(agent, fresh)

    assert hook.calls == [(fresh, SESSION_ID, {})]


@needs_session_db
@pytest.mark.parametrize("kind", ["positional-only", "raising", "keep-live"])
def test_real_session_db_adoption_hook_boundary(kind, tmp_path):
    for name in ("old", "fresh"):
        (tmp_path / name).mkdir()
    old = SessionDB(db_path=tmp_path / "old" / "state.db")
    fresh = SessionDB(db_path=tmp_path / "fresh" / "state.db")
    hook = {"positional-only": PositionalOnlyHook, "raising": RaisingHook}.get(kind, KeywordHook)()
    agent = _agent(old, hook)
    try:
        if kind != "keep-live":
            old.close()
        assert streaming._session_db_is_open(old) == (kind == "keep-live")

        result = streaming._adopt_session_db_for_cached_agent(agent, fresh)

        if kind == "keep-live":
            assert result is old and agent._session_db is old
            assert not streaming._session_db_is_open(fresh)
            assert hook.calls == []
        else:
            assert result is fresh and agent._session_db is fresh
            assert streaming._session_db_is_open(fresh)
            assert [call[:2] for call in hook.calls] == [(fresh, SESSION_ID)]
    finally:
        old.close()
        fresh.close()
