from __future__ import annotations

from types import SimpleNamespace


def _build_real_compression_chain(tmp_path):
    import pytest

    hermes_state = pytest.importorskip("hermes_state")
    SessionDB = hermes_state.SessionDB

    db = SessionDB(tmp_path / "state.db")
    db.create_session("parent", source="webui")
    db.end_session("parent", "compression")

    # Decoy children share parent_session_id but are not compression successors.
    db.create_session(
        "delegate-child",
        source="webui",
        parent_session_id="parent",
        model_config={"_delegate_from": "parent"},
    )
    db.create_session("tool-child", source="tool", parent_session_id="parent")

    db.create_session("middle", source="webui", parent_session_id="parent")
    db.end_session("middle", "compression")
    db.create_session("live-webui", source="webui", parent_session_id="middle")
    return db


def test_wakeup_target_follows_real_canonical_compression_chain(monkeypatch, tmp_path):
    """A wakeup addressed to a sealed snapshot lands on its live WebUI tip."""
    import api.background_process as background_process
    import api.routes as routes
    import api.state_sync as state_sync

    db = _build_real_compression_chain(tmp_path)
    monkeypatch.setattr(
        routes,
        "_get_or_materialize_session",
        lambda session_id, **kwargs: SimpleNamespace(
            session_id=session_id,
            profile="default",
            pre_compression_snapshot=True,
        ),
    )
    monkeypatch.setattr(state_sync, "_get_state_db", lambda profile=None: db)

    assert db.get_compression_tip("parent") == "live-webui"
    assert background_process._canonical_wakeup_session_id("parent") == "live-webui"
    assert db._conn is None


def test_wakeup_target_uses_full_chain_resolver_without_agent_install(monkeypatch):
    """The standalone WebUI contract consumes SessionDB's full-chain tip API."""
    import api.background_process as background_process
    import api.routes as routes
    import api.state_sync as state_sync

    class ContractDB:
        def __init__(self):
            self.tip_calls = []
            self.closed = False

        def get_compression_tip(self, session_id):
            self.tip_calls.append(session_id)
            return "live-webui"

        def get_session(self, session_id):
            assert session_id == "live-webui"
            return {"id": session_id, "ended_at": None}

        def close(self):
            self.closed = True

    db = ContractDB()
    monkeypatch.setattr(
        routes,
        "_get_or_materialize_session",
        lambda session_id, **kwargs: SimpleNamespace(
            session_id=session_id,
            profile="default",
            pre_compression_snapshot=True,
        ),
    )
    monkeypatch.setattr(state_sync, "_get_state_db", lambda profile=None: db)

    assert background_process._canonical_wakeup_session_id("parent") == "live-webui"
    assert db.tip_calls == ["parent"]
    assert db.closed is True


def test_wakeup_target_keeps_live_origin_without_opening_state_db(monkeypatch):
    """Ordinary live sessions retain the exact origin routing contract."""
    import api.background_process as background_process
    import api.routes as routes
    import api.state_sync as state_sync

    monkeypatch.setattr(
        routes,
        "_get_or_materialize_session",
        lambda session_id, **kwargs: SimpleNamespace(
            session_id=session_id,
            profile="default",
            pre_compression_snapshot=False,
        ),
    )
    monkeypatch.setattr(
        state_sync,
        "_get_state_db",
        lambda profile=None: (_ for _ in ()).throw(AssertionError("DB must stay unopened")),
    )

    assert background_process._canonical_wakeup_session_id("live-origin") == "live-origin"
