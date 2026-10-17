"""Pin/unpin reach state.db when the Hermes home path holds URI-special characters."""

import sqlite3

import pytest

from tests._pin_helpers import PinSess, db_pins, install_sqlite_session_db, patch_pin_endpoint


@pytest.mark.parametrize("home_name", ["ho#me", "pc%41t", "q?x"])
def test_pin_and_unpin_write_state_db_under_uri_special_home(tmp_path, monkeypatch, home_name):
    from api import routes, state_sync

    home = tmp_path / home_name / ".hermes"
    home.mkdir(parents=True)
    db = home / "state.db"
    conn = sqlite3.connect(str(db))
    conn.execute("CREATE TABLE sessions (id TEXT PRIMARY KEY, pinned INTEGER NOT NULL DEFAULT 0)")
    conn.execute("INSERT INTO sessions VALUES ('s1', 0)")
    conn.commit()
    conn.close()
    before = sorted(p.name for p in tmp_path.rglob("*"))

    install_sqlite_session_db(monkeypatch)
    monkeypatch.setattr(state_sync, "_resolve_state_db_path", lambda profile=None: db)
    monkeypatch.setattr(state_sync, "_get_state_db", lambda profile=None: state_sync_db(db))
    monkeypatch.setattr(routes, "_pin_quota_rows_from_state_db", lambda rows, *_a: [dict(r) for r in rows])
    monkeypatch.setattr(routes, "list_profiles_api", lambda: [{"name": "default", "is_default": True}])

    assert state_sync.state_db_knows_session("s1", profile="default") is True
    sess = PinSess("s1")
    post = patch_pin_endpoint(monkeypatch, {"s1": sess})
    assert post("s1", True)[0] == 200
    assert db_pins(db) == {"s1": True}
    assert post("s1", False)[0] == 200
    assert db_pins(db) == {"s1": False}
    # No stray database was created at a truncated/decoded path.
    assert sorted(p.name for p in tmp_path.rglob("*")) == before


def state_sync_db(db):
    import sys
    return sys.modules["hermes_state"].SessionDB(db)
