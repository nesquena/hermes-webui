from __future__ import annotations

import json
from types import SimpleNamespace

import pytest


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


def _write_session_sidecar(
    session_dir, filename_session_id, *, payload_session_id, profile, snapshot
):
    payload = {
        "session_id": payload_session_id,
        "title": payload_session_id,
        "workspace": str(session_dir),
        "model": "test-model",
        "messages": [],
        "created_at": 1.0,
        "updated_at": 1.0,
        "profile": profile,
        "pre_compression_snapshot": snapshot,
    }
    (session_dir / f"{filename_session_id}.json").write_text(
        json.dumps(payload), encoding="utf-8"
    )


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


@pytest.mark.parametrize(
    ("session_profile", "expected_owner"),
    [(None, "default"), ("", "default"), ("named-profile", "named-profile")],
)
def test_wakeup_target_pins_explicit_profile_owner(
    monkeypatch, session_profile, expected_owner
):
    """Background routing never resolves an archived origin through TLS state."""
    import api.background_process as background_process
    import api.routes as routes
    import api.state_sync as state_sync

    class OwnedDB:
        def __init__(self):
            self.closed = False

        def get_compression_tip(self, session_id):
            assert session_id == "parent"
            return "owned-live-tip"

        def get_session(self, session_id):
            assert session_id == "owned-live-tip"
            return {"id": session_id, "ended_at": None}

        def close(self):
            self.closed = True

    db = OwnedDB()
    requested_profiles = []
    monkeypatch.setattr(
        routes,
        "_get_or_materialize_session",
        lambda session_id, **kwargs: SimpleNamespace(
            session_id=session_id,
            profile=session_profile,
            pre_compression_snapshot=True,
        ),
    )

    def get_owned_db(profile=None):
        requested_profiles.append(profile)
        return db

    monkeypatch.setattr(state_sync, "_get_state_db", get_owned_db)

    assert background_process._canonical_wakeup_session_id("parent") == "owned-live-tip"
    assert requested_profiles == [expected_owner]
    assert db.closed is True


def test_wakeup_target_accepts_only_the_expected_sid_across_live_profiles(
    monkeypatch, tmp_path
):
    """A foreign active profile cannot supply the archived origin snapshot."""
    import api.background_process as background_process
    import api.models as models
    import api.state_sync as state_sync

    session_dir = tmp_path / "sessions"
    session_dir.mkdir()
    monkeypatch.setattr(models, "SESSION_DIR", session_dir)
    monkeypatch.setattr(models, "SESSION_INDEX_FILE", session_dir / "_index.json")
    models.SESSIONS.clear()

    class RoutingDB:
        def __init__(self, tip):
            self.tip = tip
            self.closed = False

        def get_compression_tip(self, session_id):
            assert session_id == "parent"
            return self.tip

        def get_session(self, session_id):
            assert session_id == self.tip
            return {"id": session_id, "ended_at": None}

        def close(self):
            self.closed = True

    owner_db = RoutingDB("owner-live-tip")
    foreign_db = RoutingDB("foreign-live-tip")
    requested_profiles = []

    def get_owned_db(profile=None):
        requested_profiles.append(profile)
        assert profile in {"default", "foreign"}
        return {"default": owner_db, "foreign": foreign_db}[profile]

    monkeypatch.setattr(state_sync, "_get_state_db", get_owned_db)

    # Model the unsafe owner-only fallback: the file selected for the expected
    # origin contains a terminal snapshot owned by another live session/profile.
    _write_session_sidecar(
        session_dir,
        "parent",
        payload_session_id="foreign-session",
        profile="foreign",
        snapshot=True,
    )
    assert background_process._canonical_wakeup_session_id("parent") == ""
    assert requested_profiles == []

    # The exact owner remains routable while both profile DBs have live tips.
    _write_session_sidecar(
        session_dir,
        "parent",
        payload_session_id="parent",
        profile=None,
        snapshot=True,
    )
    models.SESSIONS.clear()
    assert background_process._canonical_wakeup_session_id("parent") == "owner-live-tip"
    assert requested_profiles == ["default"]
    assert owner_db.closed is True
    assert foreign_db.closed is False


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
