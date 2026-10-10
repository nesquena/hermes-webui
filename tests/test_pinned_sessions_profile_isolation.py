"""An explicit-profile pin read never falls back to another profile's state.db."""

import sqlite3


def _db(path, pinned):
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(path))
    conn.execute("CREATE TABLE sessions (id TEXT PRIMARY KEY, pinned INTEGER NOT NULL DEFAULT 0)")
    conn.execute("INSERT INTO sessions VALUES ('collision', ?)", (int(pinned),))
    conn.commit()
    conn.close()


def _homes(tmp_path, monkeypatch):
    import api.profiles as profiles

    active = tmp_path / "active"
    work = tmp_path / "work"
    active.mkdir()
    work.mkdir()
    homes = {"default": active, "work": work}
    monkeypatch.setattr(profiles, "_resolve_profile_home_for_name", lambda n: homes.get(n or "default", active))
    monkeypatch.setattr(profiles, "get_hermes_home_for_profile", lambda n: homes.get(n or "default", active))
    monkeypatch.setattr(profiles, "_is_root_profile", lambda n: n == "default")
    monkeypatch.setattr(profiles, "get_active_hermes_home", lambda: active)
    return active, work


def test_pin_flags_for_profile_without_state_db_are_empty(tmp_path, monkeypatch):
    from api import models

    active, _work = _homes(tmp_path, monkeypatch)
    _db(active / "state.db", pinned=True)

    assert models.agent_session_pinned_flags(["collision"], profile="default") == {"collision": True}
    # "work" has no state.db: the active profile's same-id row must not leak in.
    assert models.agent_session_pinned_flags(["collision"], profile="work") == {}


def test_pin_flags_read_each_profile_own_row(tmp_path, monkeypatch):
    from api import models

    active, work = _homes(tmp_path, monkeypatch)
    _db(active / "state.db", pinned=True)
    _db(work / "state.db", pinned=False)

    assert models.agent_session_pinned_flags(["collision"], profile="work") == {"collision": False}
    assert models.agent_session_pinned_flags(["collision"], profile="default") == {"collision": True}
