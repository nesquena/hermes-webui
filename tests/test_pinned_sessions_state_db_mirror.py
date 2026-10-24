"""Pins are WebUI sidecar data, mirrored best-effort into the Agent's state.db.

``/api/session/pin`` saves the sidecar exactly as before and the response depends
only on that save. The write into ``state.db.sessions.pinned`` -- the flag Hermes
Desktop and ``hermes sessions pin`` share -- is best-effort: no column, no row, or
a writer holding the database costs the other app the update, never this request.
The sidebar reads that column as an extra input, so a pin made in Desktop or the
CLI shows up here without ever clearing a WebUI pin.
"""

import pathlib
import sqlite3
import threading
import time
from types import SimpleNamespace

ROOT = pathlib.Path(__file__).resolve().parent.parent


# ── fixtures ─────────────────────────────────────────────────────────────────

def _make_state_db(path, *, rows=(("s1", 0),), pinned_column=True):
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(path))
    try:
        if pinned_column:
            conn.execute("CREATE TABLE sessions (id TEXT PRIMARY KEY, pinned INTEGER NOT NULL DEFAULT 0)")
            conn.executemany("INSERT INTO sessions (id, pinned) VALUES (?, ?)", list(rows))
        else:
            conn.execute("CREATE TABLE sessions (id TEXT PRIMARY KEY)")
            conn.executemany("INSERT INTO sessions (id) VALUES (?)", [(sid,) for sid, *_ in rows])
        conn.commit()
    finally:
        conn.close()


def _db_snapshot(path):
    """``(columns, {id: pinned})`` of a state.db, or bytes when there is no pin column."""
    conn = sqlite3.connect(str(path))
    try:
        columns = {row[1] for row in conn.execute("PRAGMA table_info(sessions)")}
        pins = (
            {row[0]: bool(row[1]) for row in conn.execute("SELECT id, pinned FROM sessions")}
            if "pinned" in columns else None
        )
        return columns, pins
    finally:
        conn.close()


def _homes(tmp_path, monkeypatch, names=("default",)):
    """Give each profile its own home directory; return ``{name: Path}``."""
    import api.profiles as profiles

    homes = {}
    for name in names:
        homes[name] = tmp_path / name
        homes[name].mkdir(parents=True, exist_ok=True)
    monkeypatch.setattr(
        profiles, "_resolve_profile_home_for_name",
        lambda name: str(homes.get(name or "default", homes["default"])),
    )
    monkeypatch.setattr(profiles, "get_active_hermes_home", lambda: str(homes["default"]))
    monkeypatch.setattr(profiles, "_is_root_profile", lambda name: (name or "default") == "default")
    return homes


class _PinSession:
    """Sidecar session fake: ``save()`` is all the pin route needs from it."""

    archived = False

    def __init__(self, sid, profile="default"):
        self.session_id, self.profile, self.pinned = sid, profile, False

    def compact(self):
        return {"session_id": self.session_id, "pinned": bool(self.pinned), "profile": self.profile}

    def save(self, touch_updated_at=True):
        self.saved = bool(self.pinned)


def _pin_endpoint(monkeypatch, sessions, *, limit=10):
    """Stub the HTTP envelope of ``/api/session/pin``; return ``post(sid, pinned)``."""
    from api import routes

    bodies, responses, locks = {}, {}, {}
    monkeypatch.setattr(routes, "_get_session_agent_lock", lambda sid: locks.setdefault(sid, threading.RLock()))
    monkeypatch.setattr(routes, "get_session", lambda sid, **kw: sessions[sid])
    monkeypatch.setattr(routes, "_ensure_full_session_before_mutation", lambda _sid, s: s)
    monkeypatch.setattr(routes, "_session_is_subagent_view_only", lambda _sid: False)
    monkeypatch.setattr(routes, "all_sessions", lambda *a, **kw: [])
    monkeypatch.setattr(routes, "SESSIONS", {})
    monkeypatch.setattr(routes, "list_profiles_api", lambda *a, **kw: [{"name": "default", "is_default": True}])
    monkeypatch.setattr(routes, "_root_profile_names_snapshot", lambda: {"default"})
    monkeypatch.setattr(routes, "load_settings", lambda: {"pinned_sessions_limit": limit})
    monkeypatch.setattr(routes, "publish_session_list_changed", lambda *a, **kw: None)
    monkeypatch.setattr(routes, "_guard_request_session_visibility", lambda *a, **kw: True)
    monkeypatch.setattr(routes, "_check_csrf", lambda handler: True)
    monkeypatch.setattr(routes, "read_body", lambda handler: bodies[threading.current_thread().name])

    def reply(status, payload):
        responses[threading.current_thread().name] = (status, payload)
        return True

    monkeypatch.setattr(routes, "j", lambda h, payload, status=200, extra_headers=None: reply(status, payload))
    monkeypatch.setattr(routes, "bad", lambda h, msg, status=400: reply(status, {"error": msg}))

    def post(sid, pinned=True):
        name = threading.current_thread().name
        bodies[name] = {"session_id": sid, "pinned": pinned}
        routes.handle_post(object(), SimpleNamespace(path="/api/session/pin"))
        return responses.pop(name)

    return post


def _sidebar_payload(monkeypatch, rows, *, active_profile="default", all_profiles=False):
    """Build the sidebar payload over *rows*, with listing and pruning stubbed out.

    Returns the row list in payload order, so two profiles that keep the same session
    id stay distinguishable.
    """
    from api import routes

    monkeypatch.setattr(
        routes, "all_sessions",
        lambda **kw: [dict(row, is_cli_session=False, read_only=False, message_count=1) for row in rows],
    )
    monkeypatch.setattr(routes, "_enrich_sidebar_lineage_metadata", lambda _rows: None)
    monkeypatch.setattr(routes, "_reconcile_stale_stream_state_for_session_rows", lambda _rows: False)
    monkeypatch.setattr(routes, "_prune_orphaned_webui_zero_message_sessions", lambda rows, **kw: list(rows))
    monkeypatch.setattr(
        routes, "get_cli_sessions",
        lambda *a, **kw: (_ for _ in ()).throw(AssertionError("CLI listing must not run")),
    )
    payload = routes._build_session_list_cache_payload(
        active_profile=active_profile,
        all_profiles=all_profiles,
        show_cli_sessions=False,
        show_previous_messaging_sessions=False,
        show_cron_sessions=False,
        include_archived=False,
    )
    return payload["sessions"]


def _sidebar_rows(monkeypatch, rows, *, active_profile="default", all_profiles=False):
    return {
        row["session_id"]: row
        for row in _sidebar_payload(
            monkeypatch, rows, active_profile=active_profile, all_profiles=all_profiles,
        )
    }


# ── the pin route never fails because of the mirror ──────────────────────────

def test_pin_and_unpin_save_the_sidecar_when_state_db_has_no_pin_column(tmp_path, monkeypatch):
    homes = _homes(tmp_path, monkeypatch)
    db = homes["default"] / "state.db"
    _make_state_db(db, rows=(("s1", 0),), pinned_column=False)
    sessions = {"s1": _PinSession("s1")}
    post = _pin_endpoint(monkeypatch, sessions)

    assert post("s1", True)[0] == 200
    assert sessions["s1"].pinned is True and sessions["s1"].saved is True
    assert post("s1", False)[0] == 200
    assert sessions["s1"].pinned is False and sessions["s1"].saved is False
    # A mirror that cannot land must not add the column the Agent did not create.
    assert "pinned" not in _db_snapshot(db)[0]


def test_pin_and_unpin_save_the_sidecar_while_another_writer_holds_state_db(tmp_path, monkeypatch):
    homes = _homes(tmp_path, monkeypatch)
    db = homes["default"] / "state.db"
    _make_state_db(db, rows=(("s1", 0),))
    sessions = {"s1": _PinSession("s1")}
    post = _pin_endpoint(monkeypatch, sessions)

    holder = sqlite3.connect(str(db), isolation_level=None)
    try:
        holder.execute("BEGIN IMMEDIATE")
        started = time.monotonic()
        status, _ = post("s1", True)
        elapsed = time.monotonic() - started
        assert status == 200
        assert sessions["s1"].saved is True
        # The wait is bounded by the short busy timeout (~0.5 s), not unbounded.
        assert elapsed < 3
        assert _db_snapshot(db)[1] == {"s1": False}
        assert post("s1", False)[0] == 200
        assert sessions["s1"].saved is False
    finally:
        holder.execute("ROLLBACK")
        holder.close()


def test_pin_and_unpin_save_the_sidecar_when_the_state_db_row_is_missing(tmp_path, monkeypatch):
    homes = _homes(tmp_path, monkeypatch)
    db = homes["default"] / "state.db"
    _make_state_db(db, rows=())
    sessions = {"s1": _PinSession("s1")}
    post = _pin_endpoint(monkeypatch, sessions)

    assert post("s1", True)[0] == 200
    assert sessions["s1"].saved is True
    assert post("s1", False)[0] == 200
    assert sessions["s1"].saved is False
    # Nothing invents the row the Agent does not have.
    assert _db_snapshot(db)[1] == {}


def test_pin_mirrors_to_state_db_and_unpin_clears_it(tmp_path, monkeypatch):
    homes = _homes(tmp_path, monkeypatch)
    db = homes["default"] / "state.db"
    _make_state_db(db, rows=(("s1", 0), ("other", 0)))
    sessions = {"s1": _PinSession("s1")}
    post = _pin_endpoint(monkeypatch, sessions)

    assert post("s1", True)[0] == 200
    assert _db_snapshot(db)[1] == {"s1": True, "other": False}
    assert post("s1", False)[0] == 200
    assert _db_snapshot(db)[1] == {"s1": False, "other": False}


# ── the sidebar reads state.db pins as an extra input ────────────────────────

def test_desktop_pin_shows_in_the_sidebar(tmp_path, monkeypatch):
    homes = _homes(tmp_path, monkeypatch)
    _make_state_db(homes["default"] / "state.db", rows=(("s1", 1), ("s2", 0)))

    rows = _sidebar_rows(monkeypatch, [
        {"session_id": "s1", "profile": "default", "pinned": False, "session_source": "webui"},
        {"session_id": "s2", "profile": "default", "pinned": False, "session_source": "webui"},
    ])

    assert rows["s1"]["pinned"] is True
    assert rows["s2"]["pinned"] is False


def test_webui_pin_survives_a_mirror_that_did_not_land(tmp_path, monkeypatch):
    homes = _homes(tmp_path, monkeypatch)
    # state.db still says unpinned: the mirror lost the writer when the user pinned.
    _make_state_db(homes["default"] / "state.db", rows=(("s1", 0),))

    rows = _sidebar_rows(monkeypatch, [
        {"session_id": "s1", "profile": "default", "pinned": True, "session_source": "webui"},
    ])

    assert rows["s1"]["pinned"] is True


def test_sidebar_build_never_writes_state_db(tmp_path, monkeypatch):
    homes = _homes(tmp_path, monkeypatch)
    db = homes["default"] / "state.db"
    _make_state_db(db, rows=(("s1", 1), ("s2", 0)))
    before = (db.read_bytes(), _db_snapshot(db))

    rows = _sidebar_rows(monkeypatch, [
        {"session_id": "s1", "profile": "default", "pinned": False, "session_source": "webui"},
        {"session_id": "s2", "profile": "default", "pinned": True, "session_source": "webui"},
    ])

    assert (db.read_bytes(), _db_snapshot(db)) == before
    assert {sid: row["pinned"] for sid, row in rows.items()} == {"s1": True, "s2": True}


def test_profile_a_sidebar_never_reads_or_writes_profile_b(tmp_path, monkeypatch):
    from api import state_sync

    homes = _homes(tmp_path, monkeypatch, names=("default", "work"))
    db_a, db_b = homes["default"] / "state.db", homes["work"] / "state.db"
    # The same id is pinned in B only: A must not adopt it, or even look at B.
    _make_state_db(db_a, rows=(("s1", 0), ("s2", 1)))
    _make_state_db(db_b, rows=(("s1", 1), ("b_only", 1)))
    opened, before_b = [], (db_b.read_bytes(), _db_snapshot(db_b))
    real_read = state_sync.read_state_db_pinned_ids

    def spy(profile=None):
        opened.append(str(state_sync._resolve_state_db_path(profile)))
        return real_read(profile)

    monkeypatch.setattr(state_sync, "read_state_db_pinned_ids", spy)

    rows = _sidebar_rows(monkeypatch, [
        {"session_id": "s1", "profile": "default", "pinned": False, "session_source": "webui"},
        {"session_id": "s2", "profile": "default", "pinned": False, "session_source": "webui"},
    ])

    assert opened == [str(db_a)]
    assert {sid: row["pinned"] for sid, row in rows.items()} == {"s1": False, "s2": True}
    assert (db_b.read_bytes(), _db_snapshot(db_b)) == before_b


def test_all_profiles_same_id_keeps_each_owners_own_pin(tmp_path, monkeypatch):
    """A copied/restored history id in two profiles must not borrow the other's pin."""
    homes = _homes(tmp_path, monkeypatch, names=("default", "work"))
    # A/default keeps s1 unpinned; B/work pins its own s1 and one other session.
    _make_state_db(homes["default"] / "state.db", rows=(("s1", 0),))
    _make_state_db(homes["work"] / "state.db", rows=(("s1", 1), ("b_other", 0), ("b_pinned", 1)))

    rows = _sidebar_payload(monkeypatch, [
        {"session_id": "s1", "profile": "default", "pinned": False, "session_source": "webui"},
        {"session_id": "b_other", "profile": "work", "pinned": False, "session_source": "webui"},
        {"session_id": "b_pinned", "profile": "work", "pinned": False, "session_source": "webui"},
    ], all_profiles=True)

    assert {(row["profile"], row["session_id"]): row["pinned"] for row in rows} == {
        ("default", "s1"): False,
        ("work", "b_other"): False,
        ("work", "b_pinned"): True,
    }


def test_all_profiles_same_id_marks_only_the_owning_row(tmp_path, monkeypatch):
    """One row per owner, same id: only work's row renders pinned."""
    homes = _homes(tmp_path, monkeypatch, names=("default", "work"))
    _make_state_db(homes["default"] / "state.db", rows=(("s1", 0),))
    _make_state_db(homes["work"] / "state.db", rows=(("s1", 1),))

    rows = _sidebar_payload(monkeypatch, [
        {"session_id": "s1", "profile": "default", "pinned": False, "session_source": "webui"},
        {"session_id": "s1", "profile": "work", "pinned": False, "session_source": "webui"},
    ], all_profiles=True)

    assert {(row["profile"], row["session_id"]): row["pinned"] for row in rows} == {
        ("default", "s1"): False,
        ("work", "s1"): True,
    }


def test_renamed_root_and_default_rows_share_one_pin_owner(tmp_path, monkeypatch):
    """Root/default alias equivalence survives the owner-keyed read."""
    import api.profiles as profiles

    homes = _homes(tmp_path, monkeypatch, names=("default", "work"))
    monkeypatch.setattr(profiles, "_is_root_profile", lambda name: (name or "default") in ("default", "kinni"))
    monkeypatch.setattr(
        profiles, "_resolve_profile_home_for_name",
        lambda name: str(homes["default"] if (name or "default") in ("default", "kinni") else homes["work"]),
    )
    _make_state_db(homes["default"] / "state.db", rows=(("s1", 0), ("root_pinned", 1)))
    _make_state_db(homes["work"] / "state.db", rows=(("w_pinned", 1),))

    rows = _sidebar_payload(monkeypatch, [
        {"session_id": "s1", "profile": "default", "pinned": False, "session_source": "webui"},
        {"session_id": "root_pinned", "profile": "kinni", "pinned": False, "session_source": "webui"},
        {"session_id": "w_pinned", "profile": "work", "pinned": False, "session_source": "webui"},
    ], all_profiles=True)

    assert {(row["profile"], row["session_id"]): row["pinned"] for row in rows} == {
        ("default", "s1"): False,
        ("kinni", "root_pinned"): True,
        ("work", "w_pinned"): True,
    }


def test_agent_projection_carries_the_state_db_pin(tmp_path, monkeypatch):
    from api import models

    db = tmp_path / "state.db"
    conn = sqlite3.connect(str(db))
    try:
        conn.executescript(
            """
            CREATE TABLE sessions (id TEXT PRIMARY KEY, source TEXT, title TEXT, model TEXT,
                                   started_at REAL NOT NULL, message_count INTEGER DEFAULT 0,
                                   pinned INTEGER NOT NULL DEFAULT 0);
            CREATE TABLE messages (id TEXT PRIMARY KEY, session_id TEXT, role TEXT,
                                   content TEXT, timestamp REAL);
            """
        )
        for sid, pinned in (("cli_pinned", 1), ("cli_plain", 0)):
            conn.execute(
                "INSERT INTO sessions (id, source, title, model, started_at, message_count, pinned)"
                " VALUES (?, 'cli', ?, 'openai/gpt-5', 1.0, 2, ?)",
                (sid, sid, pinned),
            )
            conn.execute(
                "INSERT INTO messages (id, session_id, role, content, timestamp)"
                " VALUES (?, ?, 'user', 'hi', 1.5)",
                (f"m_{sid}", sid),
            )
        conn.commit()
    finally:
        conn.close()

    monkeypatch.setattr(models, "get_last_workspace", lambda *_a, **_kw: str(tmp_path))
    monkeypatch.setattr(models, "ensure_cron_project", lambda *_a, **_kw: None, raising=False)
    monkeypatch.setattr(models, "ensure_webhook_project", lambda *_a, **_kw: None, raising=False)
    monkeypatch.setattr(models, "_profile_has_user_projects", lambda *_a, **_kw: False, raising=False)

    rows = {row["session_id"]: row for row in models._load_cli_sessions_uncached(tmp_path, db, None)}

    assert rows["cli_pinned"]["pinned"] is True
    assert rows["cli_plain"]["pinned"] is False
