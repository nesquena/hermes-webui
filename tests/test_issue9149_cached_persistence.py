"""Cached-agent storage compatibility, including the #9149 handle lifetime.

The stand-ins mirror Agent main's SessionDB shim (_closed/_store) and lazy
Persistence handle (_release). They run in standalone WebUI CI; the optional
real-assembly cases below additionally verify the external Agent boundary.
"""
import ast
from pathlib import Path
from types import SimpleNamespace

import pytest

from api import streaming


class Handle:
    def __init__(self):
        self._release = lambda: None
        self.close_calls = 0

    @property
    def coordination(self):
        if self._release is None:
            raise RuntimeError("persistence handle used after close")
        return self

    @property
    def ready(self):
        raise AssertionError("a liveness check must not bind the lazy backend")

    def close(self):
        self.close_calls += 1
        self._release = None


class SessionDB:
    def __init__(self):
        self._closed = False
        self._store = Handle()
        self.close_calls = 0

    def close(self):
        self.close_calls += 1
        self._closed = True
        if self._store is not None:
            self._store.close()
            self._store = None


class Compressor:
    def bind_session_state(self, session_db=None, session_id="", *, persistence=None):
        self.session_db = session_db
        self.session_id = session_id
        self.persistence = persistence


def agent_for(db):
    persistence = db._store if isinstance(db, SessionDB) else db
    compressor = Compressor()
    compressor.bind_session_state(db, "cached-session", persistence=persistence)
    return SimpleNamespace(_session_db=db, persistence=persistence,
                           session_id="cached-session", context_compressor=compressor)


@pytest.mark.parametrize("factory", [SessionDB, Handle])
def test_second_and_later_turns_keep_live_handle_and_release_unused(factory):
    old = factory()
    agent = agent_for(old)
    retained = agent.persistence
    child = retained  # reference retained by a background consumer
    for _ in range(20):
        new = factory()
        assert streaming._session_db_is_open(old)
        assert streaming._adopt_session_db_for_cached_agent(agent, new) is old
        assert agent._session_db is old
        assert agent.persistence is retained
        assert child.coordination is retained
        assert new.close_calls == 1
        assert not streaming._session_db_is_open(new)
    assert old.close_calls == 0


@pytest.mark.parametrize("factory", [SessionDB, Handle])
def test_closed_handle_replacement_rebinds_agent_and_compressor(factory):
    old = factory()
    agent = agent_for(old)
    old.close()
    new = factory()
    expected = new._store if isinstance(new, SessionDB) else new
    assert not streaming._session_db_is_open(old)
    assert streaming._adopt_session_db_for_cached_agent(agent, new) is new
    assert agent._session_db is new
    assert agent.persistence is expected
    assert agent.persistence.coordination is expected
    assert agent.context_compressor.session_db is new
    assert agent.context_compressor.persistence is expected
    assert agent.context_compressor.session_id == "cached-session"
    assert new.close_calls == 0


@pytest.mark.parametrize("rebuild_ok", [False, True])
def test_credential_self_heal_keeps_live_shim(monkeypatch, rebuild_ok):
    old, new = SessionDB(), SessionDB()
    child = old._store
    assert child is not None
    monkeypatch.setattr(streaming, "_build_session_db_for_stream",
                        lambda _path: new if rebuild_ok else None)
    kwargs = {"session_db": old}
    assert streaming._replace_session_db_in_kwargs(kwargs, "unused") is old
    assert kwargs["session_db"] is old
    assert child.coordination is child
    assert old.close_calls == 0
    assert new.close_calls == int(rebuild_ok)


@pytest.mark.parametrize("rebuild_ok", [False, True])
def test_credential_self_heal_replaces_closed_shim(monkeypatch, rebuild_ok):
    old, new = SessionDB(), SessionDB()
    old.close()
    monkeypatch.setattr(streaming, "_build_session_db_for_stream",
                        lambda _path: new if rebuild_ok else None)
    kwargs = {"session_db": old}
    expected = new if rebuild_ok else None
    assert streaming._replace_session_db_in_kwargs(kwargs, "unused") is expected
    assert kwargs["session_db"] is expected


def test_legacy_agent_and_compressor_without_persistence_keyword():
    class LegacyDB:
        def __init__(self, conn):
            self._conn = conn

        def close(self):
            self._conn = None

    class LegacyCompressor:
        def bind_session_state(self, session_db=None, session_id=""):
            self.session_db = session_db
            self.session_id = session_id

    old, new = LegacyDB(None), LegacyDB(object())
    compressor = LegacyCompressor()
    agent = SimpleNamespace(_session_db=old, session_id="legacy", context_compressor=compressor)
    assert streaming._adopt_session_db_for_cached_agent(agent, new) is new
    assert compressor.session_db is new
    assert compressor.session_id == "legacy"
    assert not hasattr(agent, "persistence")


def test_missing_same_and_unknown_handles():
    assert not streaming._session_db_is_open(None)
    assert not streaming._session_db_is_open(object())
    new = SessionDB()
    agent = agent_for(new)
    assert streaming._adopt_session_db_for_cached_agent(agent, new) is new
    assert streaming._adopt_session_db_for_cached_agent(agent, None) is new
    assert new.close_calls == 0
    agent._session_db = None
    assert streaming._adopt_session_db_for_cached_agent(agent, new) is new
    assert agent.persistence is new._store


@pytest.mark.parametrize("accepts_session_db", [False, True])
def test_cached_reuse_keeps_constructor_kwargs_on_adopted_handle(monkeypatch, accepts_session_db):
    # Execute the unmodified production reuse statement, not a handwritten
    # mirror: the later credential self-heal consumes these constructor kwargs.
    tree = ast.parse(Path(streaming.__file__).read_text(encoding="utf-8"))
    reuse = next(node for node in ast.walk(tree) if (
        isinstance(node, ast.If) and isinstance(node.test, ast.Compare)
        and isinstance(node.test.left, ast.Name) and node.test.left.id == "_session_db"
        and any(isinstance(call, ast.Call) and isinstance(call.func, ast.Name)
                and call.func.id == "_adopt_session_db_for_cached_agent"
                for call in ast.walk(node))
    ))
    old, fresh = SessionDB(), SessionDB()
    agent = agent_for(old)
    kwargs = {"session_db": fresh} if accepts_session_db else {}
    namespace = {"agent": agent, "_session_db": fresh, "_agent_kwargs": kwargs,
                 "_adopt_session_db_for_cached_agent": streaming._adopt_session_db_for_cached_agent}
    exec(compile(ast.Module(body=[reuse], type_ignores=[]), streaming.__file__, "exec"), namespace)
    assert agent._session_db is old
    assert fresh.close_calls == 1
    if accepts_session_db:
        assert kwargs["session_db"] is old
        monkeypatch.setattr(streaming, "_build_session_db_for_stream", lambda _path: None)
        assert streaming._replace_session_db_in_kwargs(kwargs, "unused") is old
        assert agent.persistence.coordination is agent.persistence
    else:
        assert "session_db" not in kwargs


def test_real_lazy_handle_liveness_does_not_open_backend(tmp_path):
    assembly = pytest.importorskip("persistence_assembly")
    handle = assembly.open_persistence(assembly.profile_scope(home=tmp_path))
    try:
        assert handle._store is None
        assert streaming._session_db_is_open(handle)
        assert handle._store is None
        assert not (tmp_path / "state.db").exists()
    finally:
        handle.close()
    assert not streaming._session_db_is_open(handle)


def test_real_pending_shim_close_does_not_release_inflight_consumer(tmp_path):
    pytest.importorskip("persistence_assembly")
    import hermes_state
    if not hasattr(hermes_state, "_SessionDB"):
        pytest.skip("Agent does not provide the new SessionDB shim")
    old = hermes_state.SessionDB(db_path=tmp_path / "state.db")
    new = hermes_state.SessionDB(db_path=tmp_path / "state.db")
    retained = old._store
    agent = SimpleNamespace(_session_db=old, persistence=retained, session_id="pending")
    # Hold the same admission used by the shim's decorated methods. Its owner
    # has already requested close; adoption must not release this active call.
    old._enter()
    try:
        old.close()
        assert old._closed and old._store is retained
        assert not streaming._session_db_is_open(old)
        assert streaming._adopt_session_db_for_cached_agent(agent, new) is new
        assert retained.coordination is not None
        assert agent.persistence is new._store
    finally:
        old._leave()
        old.close()
        new.close()
    with pytest.raises(RuntimeError, match="persistence handle used after close"):
        _ = retained.coordination


@pytest.mark.parametrize("kind", ["direct", "shim"])
def test_real_agent_persistence_survives_cached_turns_and_replacement(tmp_path, kind):
    assembly = pytest.importorskip("persistence_assembly")
    shim_type = None
    if kind == "shim":
        import hermes_state
        # Older Agent versions do not have the new shim interface.
        if not hasattr(hermes_state, "_SessionDB"):
            pytest.skip("Agent does not provide the new SessionDB shim")
        shim_type = hermes_state.SessionDB

    def factory():
        if kind == "direct":
            return assembly.open_persistence(assembly.profile_scope(home=tmp_path))
        assert shim_type is not None
        return shim_type(db_path=tmp_path / "state.db")

    def interface(db):
        return db if kind == "direct" else db._store

    old = factory()
    agent = SimpleNamespace(_session_db=old, persistence=interface(old), session_id="real")
    agent.context_compressor = Compressor()
    try:
        for _ in range(3):
            new = factory()
            try:
                assert streaming._adopt_session_db_for_cached_agent(agent, new) is old
                assert agent.persistence.coordination is not None
            finally:
                new.close()
        old.close()
        replacement = factory()
        try:
            streaming._adopt_session_db_for_cached_agent(agent, replacement)
            assert agent.persistence is interface(replacement)
            assert agent.persistence.coordination is not None
            assert agent.context_compressor.persistence is agent.persistence
        finally:
            replacement.close()
    finally:
        old.close()
