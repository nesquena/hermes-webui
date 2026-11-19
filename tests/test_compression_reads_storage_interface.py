"""Compression lineage reads on every Agent core (real Agent; skipped without one).

Newer cores turn ``hermes_state.SessionDB`` into a deprecated stand-in without
``get_compression_lineage``/``get_compression_tip``; WebUI then reads the lineage
through the storage interface. The store here is written by the installed
Agent's own session API, so the same assertions hold on old and new cores.
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest

pytestmark = pytest.mark.filterwarnings("ignore::DeprecationWarning")


@pytest.fixture
def compressed(tmp_path):
    SessionDB = pytest.importorskip("hermes_state").SessionDB
    db = SessionDB(tmp_path / "state.db")
    db.create_session("root", "webui", profile_name="default")
    db.end_session("root", "compression")
    db.create_session("tip", "webui", parent_session_id="root", profile_name="default")
    db.close()
    return tmp_path


def test_lineage_reads_match_the_old_session_db_contract(compressed):
    from api.compression_continuation import open_compression_reads

    db = open_compression_reads(compressed / "state.db")
    try:
        assert db.get_compression_lineage("tip") == ["root", "tip"]
        assert db.get_compression_tip("root") == "tip"
        assert db.get_compression_tip("tip") == "tip"
        assert db.get_compression_lineage("unknown") == []
        assert db.get_session("unknown") is None
        row = db.get_session("root")
        assert (row["id"], row["source"], row["end_reason"]) == ("root", "webui", "compression")
    finally:
        db.close()


def test_durable_continuation_resolves_the_tip(compressed, monkeypatch):
    from api import profiles
    from api.compression_continuation import durable_compression_continuation

    monkeypatch.setattr(profiles, "_resolve_profile_home_for_name", lambda _name: str(compressed))
    session = SimpleNamespace(session_id="root", profile="default")
    assert durable_compression_continuation(session) == (True, "tip")
    assert durable_compression_continuation(SimpleNamespace(session_id="tip", profile="default")) == (False, None)


def test_compressed_messaging_owner_is_verified(tmp_path):
    SessionDB = pytest.importorskip("hermes_state").SessionDB
    from api.gateway_sessions import _verified_messaging_owner

    db = SessionDB(tmp_path / "state.db")
    db.create_session("root", "telegram", session_key="telegram:chat", profile_name="work")
    db.end_session("root", "compression")
    db.create_session("tip", "telegram", session_key="telegram:chat", parent_session_id="root", profile_name="work")
    db.close()
    assert _verified_messaging_owner(str(tmp_path), "tip", "root") is True
    assert _verified_messaging_owner(str(tmp_path), "tip", "root", recovering=True) is True
    assert _verified_messaging_owner(str(tmp_path), "tip", "someone-else") is False
    assert _verified_messaging_owner(str(tmp_path / "missing"), "tip", "root") is False
