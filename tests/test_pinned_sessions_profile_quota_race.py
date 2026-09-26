"""Pin state: legacy root rows, archived quota rows, and reconcile vs a concurrent unpin."""

import sqlite3

from tests._pin_helpers import PinSess, db_pins, install_sqlite_session_db, patch_pin_endpoint


def _db(path, rows, *, archived=()):
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(path))
    conn.execute(
        "CREATE TABLE sessions (id TEXT PRIMARY KEY, source TEXT, parent_session_id TEXT,"
        " end_reason TEXT, archived INTEGER NOT NULL DEFAULT 0, pinned INTEGER NOT NULL DEFAULT 0)"
    )
    conn.executemany(
        "INSERT INTO sessions (id, source, archived, pinned) VALUES (?, ?, ?, ?)",
        [(sid, src, int(sid in archived), int(pinned)) for sid, src, pinned in rows],
    )
    conn.commit()
    conn.close()


def _two_homes(tmp_path, monkeypatch):
    """Root profile "default" plus a named profile "work" that is the ACTIVE one."""
    import api.profiles as profiles
    from api import routes

    root, work = tmp_path / "root", tmp_path / "work"
    homes = {"default": root, "work": work}
    monkeypatch.setattr(profiles, "_resolve_profile_home_for_name", lambda n: homes.get(n or "default", root))
    monkeypatch.setattr(profiles, "_is_root_profile", lambda n: n == "default")
    monkeypatch.setattr(routes, "_is_root_profile", lambda n: n == "default")
    monkeypatch.setattr(profiles, "get_active_hermes_home", lambda: work)
    monkeypatch.setattr(routes, "SESSION_DIR", tmp_path / "sessions")
    (tmp_path / "sessions").mkdir()
    install_sqlite_session_db(monkeypatch)
    return root / "state.db", work / "state.db"


def _sidecars(monkeypatch, sidecars):
    from types import SimpleNamespace
    from api import routes

    def load(sid, *a, **kw):
        sess = SimpleNamespace(pinned=sidecars[sid])
        sess.save = lambda **_kw: sidecars.__setitem__(sid, bool(sess.pinned))
        return sess

    monkeypatch.setattr(routes, "get_session", load)
    monkeypatch.setattr(routes, "_ensure_full_session_before_mutation", lambda sid, s: s)


def test_legacy_root_row_without_profile_pins_the_root_state_db(tmp_path, monkeypatch):
    from api import routes

    root_db, work_db = _two_homes(tmp_path, monkeypatch)
    _db(root_db, [("collision", "webui", False)])
    _db(work_db, [("collision", "webui", False)])
    sidecars = {"collision": True}
    _sidecars(monkeypatch, sidecars)

    # A pre-profile sidecar row has profile=None; it belongs to the root profile, not the active one.
    routes._reconcile_sidebar_pins_with_state_db([{"session_id": "collision", "pinned": True, "profile": None}])

    assert db_pins(root_db) == {"collision": True}
    assert db_pins(work_db) == {"collision": False}
    assert sidecars == {"collision": True}
    # The quota reads that pin from the root database too, once, as "default".
    monkeypatch.setattr(routes, "list_profiles_api", lambda: [{"name": "default"}, {"name": "work"}])
    rows = routes._pin_quota_rows_from_state_db([{"session_id": "collision", "pinned": False, "profile": None}])
    assert [(r["session_id"], r["pinned"], r["profile"]) for r in rows] == [("collision", True, "default")]


def test_archived_and_hidden_state_db_pins_do_not_use_quota(tmp_path, monkeypatch):
    from api import models, routes

    db = tmp_path / "state.db"
    # Three archived pins plus a pinned cron run live only in state.db.
    _db(db, [("a1", "cli", True), ("a2", "cli", True), ("a3", "cli", True), ("cron_x", "cron", True)],
        archived={"a1", "a2", "a3"})
    monkeypatch.setattr(models, "_pin_state_db_path", lambda profile=None: db)
    monkeypatch.setattr(routes, "list_profiles_api", lambda: [{"name": "default"}])
    writes = []
    monkeypatch.setattr(routes, "_write_pin_to_state_db", lambda s, p: writes.append(p) or True)
    sess = PinSess("new_pin")
    post = patch_pin_endpoint(monkeypatch, {"new_pin": sess}, limit=3)

    assert post("new_pin")[0] == 200
    assert writes == [True] and sess.pinned is True

    # Visible state.db-only pins still count.
    _db(tmp_path / "v" / "state.db", [(f"v{i}", "cli", True) for i in range(3)])
    monkeypatch.setattr(models, "_pin_state_db_path", lambda profile=None: tmp_path / "v" / "state.db")
    other = PinSess("other")
    post = patch_pin_endpoint(monkeypatch, {"other": other}, limit=3)
    assert post("other")[0] == 400


def test_reconcile_rereads_the_pin_under_the_session_lock(tmp_path, monkeypatch):
    from api import routes

    root_db, _work_db = _two_homes(tmp_path, monkeypatch)
    # Desktop pinned "s"; the WebUI sidecar is unpinned.
    _db(root_db, [("s", "webui", True)])
    sidecars = {"s": False}
    _sidecars(monkeypatch, sidecars)

    real_flags = routes.agent_session_pinned_flags
    calls = []

    def flags_then_unpin(ids, profile=None):
        out = real_flags(ids, profile=profile)
        calls.append(out)
        if len(calls) == 1:
            # After the unlocked batch read, /api/session/pin unpins "s" in both stores.
            conn = sqlite3.connect(str(root_db))
            conn.execute("UPDATE sessions SET pinned = 0 WHERE id = 's'")
            conn.commit()
            conn.close()
            sidecars["s"] = False
        return out

    monkeypatch.setattr(routes, "agent_session_pinned_flags", flags_then_unpin)
    row = {"session_id": "s", "pinned": False, "profile": "default"}
    routes._reconcile_sidebar_pins_with_state_db([row])

    assert calls[0] == {"s": True}
    assert db_pins(root_db) == {"s": False}
    assert sidecars == {"s": False}
    assert row["pinned"] is False


def test_another_profiles_sidecar_does_not_change_quota_archive_state(tmp_path, monkeypatch):
    import json

    from api import models, routes

    db = tmp_path / "state.db"
    _db(db, [(f"w{i}", "cli", True) for i in range(3)])
    # The shared sidecar store holds an archived "w0" that belongs to another profile.
    sessions = tmp_path / "sessions"
    sessions.mkdir()
    (sessions / "w0.json").write_text(json.dumps({"session_id": "w0", "profile": "default", "archived": True}))
    monkeypatch.setattr(models, "SESSION_DIR", sessions)
    models.clear_sidecar_metadata_cache()
    monkeypatch.setattr(models, "_pin_state_db_path", lambda profile=None: db if profile == "work" else None)
    monkeypatch.setattr(routes, "list_profiles_api", lambda: [{"name": "default"}, {"name": "work"}])

    rows = routes._pin_quota_rows_from_state_db([])
    assert routes._visible_pinned_lineage_ids(rows) == {"w0", "w1", "w2"}
