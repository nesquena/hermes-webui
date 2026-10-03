"""Legacy WebUI pins survive the move of the pin record into state.db.

Before state.db became the pin store, a WebUI pin lived only in the session
sidecar (``pinned: true``) while ``state.db.sessions.pinned`` stayed 0. The
first sidebar build after upgrading must copy those pins into state.db, verify
them, and mark the profile migrated before state.db is allowed to win.
"""

import sqlite3
import types

import pytest

from tests._pin_helpers import SqliteSessionDB, db_pins, install_sqlite_session_db


def _make_db(path, ids, pinned=()):
    conn = sqlite3.connect(str(path))
    conn.execute(
        "CREATE TABLE sessions (id TEXT PRIMARY KEY, pinned INTEGER NOT NULL DEFAULT 0,"
        " parent_session_id TEXT, end_reason TEXT)"
    )
    conn.executemany(
        "INSERT INTO sessions (id, pinned) VALUES (?, ?)",
        [(sid, 1 if sid in pinned else 0) for sid in ids],
    )
    conn.commit()
    conn.close()


@pytest.fixture
def upgrade_env(tmp_path, monkeypatch):
    """One profile home with a state.db and a WebUI sidecar store."""
    from api import models, routes

    home = tmp_path / "home"
    home.mkdir()
    db = home / "state.db"
    session_dir = tmp_path / "sessions"
    session_dir.mkdir()

    install_sqlite_session_db(monkeypatch)
    monkeypatch.setattr("api.profiles._resolve_profile_home_for_name", lambda _n: home, raising=False)
    monkeypatch.setattr("api.profiles._is_root_profile", lambda n: n == "default", raising=False)
    monkeypatch.setattr(models, "_get_profile_home", lambda _p: home)
    monkeypatch.setattr(routes, "SESSION_DIR", session_dir)

    sidecars = {}

    class _Sess:
        def __init__(self, sid):
            self.session_id = sid
            self.pinned = sidecars[sid]

        def save(self, **_kw):
            sidecars[self.session_id] = bool(self.pinned)

    monkeypatch.setattr(routes, "get_session", lambda sid, *a, **kw: _Sess(sid))
    monkeypatch.setattr(routes, "_ensure_full_session_before_mutation", lambda sid, s: s)
    return types.SimpleNamespace(db=db, sidecars=sidecars, session_dir=session_dir, routes=routes)


def _sidebar_build(env):
    rows = [
        {"session_id": sid, "pinned": pinned, "profile": "default"}
        for sid, pinned in env.sidecars.items()
    ]
    env.routes._reconcile_sidebar_pins_with_state_db(rows)
    return {row["session_id"]: row["pinned"] for row in rows}


def test_legacy_sidecar_pins_survive_first_sidebar_build(upgrade_env):
    env = upgrade_env
    # Pre-upgrade install: pins exist only in sidecars; state.db has pinned=0.
    _make_db(env.db, ["legacy_a", "legacy_b", "plain"])
    env.sidecars.update({"legacy_a": True, "legacy_b": True, "plain": False, "webui_only": True})

    shown = _sidebar_build(env)

    assert shown == {"legacy_a": True, "legacy_b": True, "plain": False, "webui_only": True}
    assert env.sidecars == {"legacy_a": True, "legacy_b": True, "plain": False, "webui_only": True}
    assert db_pins(env.db) == {"legacy_a": True, "legacy_b": True, "plain": False}


def test_after_migration_state_db_wins(upgrade_env):
    env = upgrade_env
    _make_db(env.db, ["legacy_a", "desktop_pin"], pinned={"desktop_pin"})
    env.sidecars.update({"legacy_a": True, "desktop_pin": False})
    _sidebar_build(env)
    assert db_pins(env.db) == {"legacy_a": True, "desktop_pin": True}

    # Desktop unpins after the migration: that now propagates to the sidecar.
    conn = sqlite3.connect(str(env.db))
    conn.execute("UPDATE sessions SET pinned = 0 WHERE id = 'legacy_a'")
    conn.commit()
    conn.close()

    shown = _sidebar_build(env)
    assert shown == {"legacy_a": False, "desktop_pin": True}
    assert env.sidecars == {"legacy_a": False, "desktop_pin": True}


def test_failed_migration_write_leaves_sidecar_pins_and_retries(upgrade_env):
    env = upgrade_env
    _make_db(env.db, ["legacy_a"])
    env.sidecars.update({"legacy_a": True})
    SqliteSessionDB.fail_writes = True

    shown = _sidebar_build(env)
    assert shown == {"legacy_a": True}
    assert env.sidecars == {"legacy_a": True}
    assert db_pins(env.db) == {"legacy_a": False}

    SqliteSessionDB.fail_writes = False
    _sidebar_build(env)
    assert db_pins(env.db) == {"legacy_a": True}
    assert env.sidecars == {"legacy_a": True}


def test_migration_is_recorded_once_per_state_db(upgrade_env):
    env = upgrade_env
    _make_db(env.db, ["legacy_a"])
    env.sidecars.update({"legacy_a": True})
    _sidebar_build(env)
    markers = list(env.session_dir.glob("_pin*migration*"))
    assert len(markers) == 1, markers

    writes = []
    orig = SqliteSessionDB.set_session_pinned

    def _count(self, sid, pinned):
        writes.append((sid, pinned))
        return orig(self, sid, pinned)

    SqliteSessionDB.set_session_pinned = _count
    try:
        _sidebar_build(env)
        _sidebar_build(env)
    finally:
        SqliteSessionDB.set_session_pinned = orig
    assert writes == []


def test_failed_pin_read_does_not_finalize_migration(upgrade_env, monkeypatch):
    from api import models

    env = upgrade_env
    _make_db(env.db, ["legacy_a"])
    env.sidecars.update({"legacy_a": True})
    real_open = models.open_state_db_readonly

    def _broken(*_a, **_kw):
        raise OSError("database is locked")

    monkeypatch.setattr(models, "open_state_db_readonly", _broken)
    assert _sidebar_build(env) == {"legacy_a": True}
    assert list(env.session_dir.glob("_pin*migration*")) == []

    # The read recovers: the migration retries and the sidecar pin reaches state.db.
    monkeypatch.setattr(models, "open_state_db_readonly", real_open)
    assert _sidebar_build(env) == {"legacy_a": True}
    assert db_pins(env.db) == {"legacy_a": True}
    assert env.sidecars == {"legacy_a": True}


def test_absent_row_pin_is_written_once_the_row_appears(upgrade_env):
    env = upgrade_env
    # The sidecar pin predates its state.db row (the Agent has not inserted it yet).
    _make_db(env.db, ["other"])
    env.sidecars.update({"late": True, "other": False})
    assert _sidebar_build(env) == {"late": True, "other": False}

    # The Agent inserts the row with its default pinned=0.
    conn = sqlite3.connect(str(env.db))
    conn.execute("INSERT INTO sessions (id, pinned) VALUES ('late', 0)")
    conn.commit()
    conn.close()

    assert _sidebar_build(env) == {"late": True, "other": False}
    assert db_pins(env.db) == {"late": True, "other": False}
    assert env.sidecars == {"late": True, "other": False}

    # Nothing pending any more: a later Desktop unpin now reaches the sidecar.
    conn = sqlite3.connect(str(env.db))
    conn.execute("UPDATE sessions SET pinned = 0 WHERE id = 'late'")
    conn.commit()
    conn.close()
    assert _sidebar_build(env) == {"late": False, "other": False}


def _compressed_lineage(env):
    _make_db(env.db, ["root", "child"], pinned={"root"})
    conn = sqlite3.connect(str(env.db))
    conn.execute("UPDATE sessions SET end_reason = 'compression' WHERE id = 'root'")
    conn.execute("UPDATE sessions SET parent_session_id = 'root' WHERE id = 'child'")
    conn.commit()
    conn.close()
    env.sidecars.update({"root": True})
    _sidebar_build(env)


def _failed_carry(env):
    from api.streaming import _carry_pin_to_compression_child

    SqliteSessionDB.fail_writes = True
    assert _carry_pin_to_compression_child("root", "child", "default") is False
    SqliteSessionDB.fail_writes = False
    env.sidecars["child"] = True  # the rotation keeps the sidecar pin


def test_failed_carry_retries_without_a_saved_marker(upgrade_env, monkeypatch):
    env = upgrade_env
    _compressed_lineage(env)
    # The marker file cannot be written either: the retry comes from state.db's lineage.
    monkeypatch.setattr(env.routes, "_save_pin_migration_state", lambda _s: False)
    _failed_carry(env)
    assert db_pins(env.db) == {"root": True, "child": False}

    assert _sidebar_build(env) == {"root": True, "child": True}
    assert db_pins(env.db) == {"root": True, "child": True}
    assert env.sidecars == {"root": True, "child": True}


def test_failed_carry_retry_keeps_a_newer_desktop_unpin(upgrade_env):
    env = upgrade_env
    _compressed_lineage(env)
    _failed_carry(env)

    # Desktop unpins the lineage before the next sidebar build.
    conn = sqlite3.connect(str(env.db))
    conn.execute("UPDATE sessions SET pinned = 0")
    conn.commit()
    conn.close()

    assert _sidebar_build(env) == {"root": False, "child": False}
    assert db_pins(env.db) == {"root": False, "child": False}
    assert env.sidecars == {"root": False, "child": False}


def test_pin_on_absent_row_after_migration_survives_the_row_default(upgrade_env):
    from tests._pin_helpers import PinSess

    env = upgrade_env
    _make_db(env.db, ["other"])
    env.sidecars.update({"other": False})
    _sidebar_build(env)  # migration marked complete

    # WebUI pins a session the Agent has not inserted into state.db yet.
    env.sidecars["late"] = True
    assert env.routes._write_pin_to_state_db(PinSess("late"), True) is True
    conn = sqlite3.connect(str(env.db))
    conn.execute("INSERT INTO sessions (id, pinned) VALUES ('late', 0)")
    conn.commit()
    conn.close()

    assert _sidebar_build(env) == {"other": False, "late": True}
    assert db_pins(env.db) == {"other": False, "late": True}
    assert env.sidecars == {"other": False, "late": True}


def test_unpin_of_absent_row_drops_its_pending_pin(upgrade_env):
    from tests._pin_helpers import PinSess

    env = upgrade_env
    _make_db(env.db, ["other"])
    env.sidecars.update({"other": False})
    _sidebar_build(env)
    env.sidecars["late"] = True
    assert env.routes._write_pin_to_state_db(PinSess("late"), True) is True
    env.sidecars["late"] = False
    assert env.routes._write_pin_to_state_db(PinSess("late"), False) is True
    conn = sqlite3.connect(str(env.db))
    conn.execute("INSERT INTO sessions (id, pinned) VALUES ('late', 0)")
    conn.commit()
    conn.close()
    assert _sidebar_build(env) == {"other": False, "late": False}
    assert db_pins(env.db) == {"other": False, "late": False}


def test_unpin_during_migration_is_not_overwritten(upgrade_env, monkeypatch):
    import threading
    from tests._pin_helpers import PinSess, patch_pin_endpoint

    env = upgrade_env
    _make_db(env.db, ["a"])
    env.sidecars["a"] = True
    sess = PinSess("a", on_save=lambda s: env.sidecars.__setitem__(s.session_id, bool(s.pinned)))
    sess.pinned = True
    post = patch_pin_endpoint(monkeypatch, {"a": sess})

    # Park the migration right after it read the pin it is about to copy.
    paused, release = threading.Event(), threading.Event()
    real_flags = env.routes.agent_session_pinned_flags

    def flags(*a, **kw):
        out = real_flags(*a, **kw)
        if threading.current_thread().name == "migration" and not paused.is_set():
            paused.set()
            release.wait(5)
        return out

    monkeypatch.setattr(env.routes, "agent_session_pinned_flags", flags)
    migration = threading.Thread(target=_sidebar_build, args=(env,), name="migration")
    migration.start()
    assert paused.wait(5)
    result = {}
    unpin = threading.Thread(target=lambda: result.update(r=post("a", False)), name="unpin")
    unpin.start()
    unpin.join(0.5)
    release.set()
    migration.join(5)
    unpin.join(5)
    assert not migration.is_alive() and not unpin.is_alive()

    assert result["r"][0] == 200
    assert env.sidecars == {"a": False}
    assert db_pins(env.db) == {"a": False}


def _rotate(env, sidecar_pinned=True):
    import api.streaming as streaming

    s = types.SimpleNamespace(pinned=sidecar_pinned)
    streaming._apply_compression_pin_carry(s, "root", "child", "default")
    env.sidecars["child"] = s.pinned
    return s.pinned


def test_compression_keeps_pin_when_parent_row_is_absent(upgrade_env):
    env = upgrade_env
    _make_db(env.db, ["other"])
    env.sidecars.update({"other": False})
    _sidebar_build(env)  # migration marked complete; neither root nor child has a row yet

    assert _rotate(env) is True
    conn = sqlite3.connect(str(env.db))
    conn.execute("INSERT INTO sessions (id, pinned) VALUES ('child', 0)")
    conn.commit()
    conn.close()
    assert _sidebar_build(env)["child"] is True
    assert db_pins(env.db)["child"] is True


def test_compression_keeps_unmigrated_legacy_pin(upgrade_env):
    env = upgrade_env
    # Legacy sidecar pin; the profile's migration has not run, so state.db still holds pinned=0.
    _make_db(env.db, ["root", "child"])
    env.sidecars["root"] = True

    assert _rotate(env) is True
    assert db_pins(env.db) == {"root": False, "child": True}
    assert _sidebar_build(env) == {"root": True, "child": True}
    assert db_pins(env.db) == {"root": True, "child": True}


def test_compression_honours_a_confirmed_unpin_after_migration(upgrade_env):
    env = upgrade_env
    _make_db(env.db, ["root", "child"], pinned=["root"])
    env.sidecars.update({"root": True, "child": False})
    _sidebar_build(env)
    conn = sqlite3.connect(str(env.db))
    conn.execute("UPDATE sessions SET pinned = 0")  # Desktop unpins after the migration
    conn.commit()
    conn.close()

    assert _rotate(env) is False
    assert db_pins(env.db) == {"root": False, "child": False}
