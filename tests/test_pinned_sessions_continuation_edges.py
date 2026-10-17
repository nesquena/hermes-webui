"""Pins follow only continuation edges, migrate without stalling the sidebar, and keep the cap
under concurrent pins (PR #7637 re-gate at 45adf9bd)."""

import json
import sqlite3
import threading
import time
from types import SimpleNamespace

import pytest

from tests._pin_helpers import PinSess, db_pins, install_sqlite_session_db, patch_pin_endpoint


@pytest.fixture
def home(tmp_path, monkeypatch):
    """A default profile whose state.db uses the installed Agent's real schema."""
    hermes_state = pytest.importorskip("hermes_state")
    import api.profiles as profiles
    from api import routes

    home = tmp_path / "home"
    home.mkdir()
    hermes_state.SessionDB(home / "state.db").close()
    monkeypatch.setattr(profiles, "_resolve_profile_home_for_name", lambda _n: home)
    monkeypatch.setattr(profiles, "_is_root_profile", lambda n: n == "default")
    monkeypatch.setattr(routes, "SESSION_DIR", tmp_path / "sessions")
    (tmp_path / "sessions").mkdir()
    return SimpleNamespace(hermes_state=hermes_state, db=home / "state.db", routes=routes)


def _fork_lineage(env):
    """Compression-ended ``root`` (pinned) -> continuation ``tip`` (pinned); ``branch`` forks root."""
    db = env.hermes_state.SessionDB(env.db)
    try:
        db.create_session("root", "webui")
        db.end_session("root", "compression")
        db.create_session("tip", "webui", parent_session_id="root")
        db.create_session("branch", "webui", parent_session_id="root",
                          model_config={"_branched_from": "root"})
    finally:
        db.close()
    conn = sqlite3.connect(env.db)
    conn.execute("UPDATE sessions SET pinned = 1 WHERE id IN ('root', 'tip')")
    conn.commit()
    conn.close()


def _sidecars(monkeypatch, routes, sidecars):
    def load(sid, *a, **kw):
        sess = SimpleNamespace(pinned=sidecars[sid])
        sess.save = lambda **_kw: sidecars.__setitem__(sid, bool(sess.pinned))
        return sess

    monkeypatch.setattr(routes, "get_session", load)
    monkeypatch.setattr(routes, "_ensure_full_session_before_mutation", lambda sid, s: s)


def _build(routes, sidecars):
    rows = [{"session_id": sid, "pinned": p, "profile": "default"} for sid, p in sidecars.items()]
    routes._reconcile_sidebar_pins_with_state_db(rows)
    return {r["session_id"]: r["pinned"] for r in rows}


def test_sidebar_build_does_not_pin_a_fork_and_fork_unpin_keeps_parent(home, monkeypatch):
    from api.models import agent_session_uncarried_pins
    from api.state_sync import sync_session_pinned

    _fork_lineage(home)
    assert db_pins(home.db) == {"root": True, "tip": True, "branch": False}
    # The fork is not an uncarried compression child.
    assert agent_session_uncarried_pins(["tip", "branch"], profile="default") == set()

    sidecars = {"root": True, "tip": True, "branch": False}
    _sidecars(monkeypatch, home.routes, sidecars)
    assert _build(home.routes, sidecars) == {"root": True, "tip": True, "branch": False}
    assert db_pins(home.db) == {"root": True, "tip": True, "branch": False}

    # Pinning then unpinning the fork never touches its parent's lineage.
    assert sync_session_pinned("branch", True, profile="default") is True
    assert db_pins(home.db) == {"root": True, "tip": True, "branch": True}
    assert sync_session_pinned("branch", False, profile="default") is True
    assert db_pins(home.db) == {"root": True, "tip": True, "branch": False}
    # The continuation is still one lineage: unpinning the tip clears the root too.
    assert sync_session_pinned("tip", False, profile="default") is True
    assert db_pins(home.db) == {"root": False, "tip": False, "branch": False}


def test_fork_of_pinned_lineage_takes_its_own_quota_slot(home):
    from api.models import agent_session_pin_lineage_rows

    _fork_lineage(home)
    conn = sqlite3.connect(home.db)
    conn.execute("UPDATE sessions SET pinned = 1 WHERE id = 'branch'")
    conn.commit()
    conn.close()
    rows = {r["session_id"]: r for r in agent_session_pin_lineage_rows({"tip", "branch"}, profile="default")}
    assert rows["tip"].get("parent_session_id") == "root"
    assert "parent_session_id" not in rows["branch"]


def test_migration_under_a_held_write_lock_returns_quickly_and_retries(home, monkeypatch):
    db = home.hermes_state.SessionDB(home.db)
    try:
        db.create_session("legacy_a", "webui")
        db.create_session("legacy_b", "webui")
    finally:
        db.close()
    sidecars = {"legacy_a": True, "legacy_b": True}
    _sidecars(monkeypatch, home.routes, sidecars)

    writer = sqlite3.connect(home.db, isolation_level=None)
    writer.execute("BEGIN IMMEDIATE")  # a concurrent Agent writer holds the lock
    try:
        started = time.monotonic()
        shown = _build(home.routes, sidecars)
        elapsed = time.monotonic() - started
    finally:
        writer.execute("ROLLBACK")
        writer.close()
    assert elapsed < 3.0, elapsed
    # Nothing is lost: the sidecar pins stand and stay pending.
    assert shown == {"legacy_a": True, "legacy_b": True}
    assert sidecars == {"legacy_a": True, "legacy_b": True}
    assert db_pins(home.db) == {"legacy_a": False, "legacy_b": False}

    # Next build, lock free: the pins land.
    assert _build(home.routes, sidecars) == {"legacy_a": True, "legacy_b": True}
    assert db_pins(home.db) == {"legacy_a": True, "legacy_b": True}


def test_confirmed_pin_of_now_existing_row_clears_its_pending_entry(tmp_path, monkeypatch):
    from api import routes

    home = tmp_path / "home"
    home.mkdir()
    db = home / "state.db"
    conn = sqlite3.connect(db)
    conn.execute("CREATE TABLE sessions (id TEXT PRIMARY KEY, pinned INTEGER NOT NULL DEFAULT 0,"
                 " parent_session_id TEXT, end_reason TEXT)")
    conn.execute("INSERT INTO sessions (id) VALUES ('other')")
    conn.commit()
    conn.close()
    install_sqlite_session_db(monkeypatch)
    monkeypatch.setattr("api.profiles._resolve_profile_home_for_name", lambda _n: home, raising=False)
    monkeypatch.setattr("api.profiles._is_root_profile", lambda n: n == "default", raising=False)
    monkeypatch.setattr(routes, "SESSION_DIR", tmp_path)
    sidecars = {"other": False}
    _sidecars(monkeypatch, routes, sidecars)
    _build(routes, sidecars)

    # Pinned while absent -> queued; the Agent then creates the row and WebUI pins it again.
    sidecars["late"] = True
    assert routes._write_pin_to_state_db(PinSess("late"), True) is True
    conn = sqlite3.connect(db)
    conn.execute("INSERT INTO sessions (id) VALUES ('late')")
    conn.commit()
    conn.close()
    assert routes._write_pin_to_state_db(PinSess("late"), True) is True
    marker = json.loads((tmp_path / routes._PIN_MIGRATION_MARKER_NAME).read_text())
    assert "late" not in marker["pending_pins"][str(db)]

    # Desktop unpins before the next sidebar build: the stale sidecar pin must not win.
    conn = sqlite3.connect(db)
    conn.execute("UPDATE sessions SET pinned = 0 WHERE id = 'late'")
    conn.commit()
    conn.close()
    assert _build(routes, sidecars) == {"other": False, "late": False}
    assert db_pins(db) == {"other": False, "late": False}


def test_concurrent_pin_rejection_cannot_let_a_stale_snapshot_bypass_the_cap(tmp_path, monkeypatch):
    """C snapshots, A commits, B's prune drops A's reservation, then C must not be admitted."""
    from api import models, routes

    db = tmp_path / "state.db"
    conn = sqlite3.connect(db)
    conn.execute("CREATE TABLE sessions (id TEXT PRIMARY KEY, source TEXT, pinned INTEGER NOT NULL DEFAULT 0)")
    conn.executemany("INSERT INTO sessions (id, source) VALUES (?, 'webui')", [("A",), ("B",), ("C",)])
    conn.commit()
    conn.close()
    monkeypatch.setattr(models, "_pin_state_db_path", lambda profile=None: db)
    monkeypatch.setattr(routes, "list_profiles_api", lambda: [{"name": "default", "is_default": True}])
    monkeypatch.setattr(routes, "_state_db_pin_confirmed", lambda *_a: True)

    def write(s, pinned):
        with sqlite3.connect(db) as c:
            c.execute("UPDATE sessions SET pinned = ? WHERE id = ?", (int(pinned), s.session_id))
        return True

    monkeypatch.setattr(routes, "_write_pin_to_state_db", write)
    sessions = {sid: PinSess(sid) for sid in "ABC"}
    post = patch_pin_endpoint(monkeypatch, sessions, limit=1)

    c_snapshot_read, a_and_b_done = threading.Event(), threading.Event()
    real_rows = routes._pin_quota_rows_from_state_db

    def rows(*a, **kw):
        out = real_rows(*a, **kw)
        if threading.current_thread().name == "C" and not c_snapshot_read.is_set():
            c_snapshot_read.set()
            assert a_and_b_done.wait(10)
        return out

    monkeypatch.setattr(routes, "_pin_quota_rows_from_state_db", rows)
    results = {}
    c = threading.Thread(target=lambda: results.__setitem__("C", post("C")), name="C")
    c.start()
    assert c_snapshot_read.wait(10)
    a = threading.Thread(target=lambda: results.__setitem__("A", post("A")), name="A")
    a.start(); a.join(10)
    b = threading.Thread(target=lambda: results.__setitem__("B", post("B")), name="B")
    b.start(); b.join(10)
    a_and_b_done.set()
    c.join(10)

    assert results["A"][0] == 200
    assert results["B"][0] == 400
    assert results["C"][0] == 400
    assert db_pins(db) == {"A": True, "B": False, "C": False}
