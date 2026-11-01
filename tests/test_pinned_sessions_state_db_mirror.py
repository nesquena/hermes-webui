"""Pins, archive and manual titles are WebUI sidecar data, mirrored best-effort into state.db.

The routes save the sidecar as before, so the mirror never fails the response. The sidebar
reads ``sessions.pinned``/``archived``: a Desktop/CLI change shows here, and a flag whose
mirror never landed is never lost (the sidecar records what it last mirrored).
"""

import collections
import sqlite3
import threading
import time
from contextlib import closing
from types import SimpleNamespace

import pytest


# ── fixtures ─────────────────────────────────────────────────────────────────

def _make_state_db(path, *, rows=(("s1", 0),), pinned_column=True):
    path.parent.mkdir(parents=True, exist_ok=True)
    with closing(sqlite3.connect(str(path))) as conn:
        if pinned_column:
            conn.execute(
                "CREATE TABLE sessions (id TEXT PRIMARY KEY, pinned INTEGER NOT NULL DEFAULT 0,"
                " archived INTEGER NOT NULL DEFAULT 0, auto_archived INTEGER NOT NULL DEFAULT 0,"
                " title TEXT, title_source TEXT, parent_session_id TEXT, end_reason TEXT)"
            )
            conn.executemany("INSERT INTO sessions (id, pinned) VALUES (?, ?)", list(rows))
        else:
            conn.execute("CREATE TABLE sessions (id TEXT PRIMARY KEY)")
            conn.executemany("INSERT INTO sessions (id) VALUES (?)", [(sid,) for sid, *_ in rows])
        conn.commit()


def _db_snapshot(path):
    """``(columns, {id: pinned})`` of a state.db, or pins=None when there is no pin column."""
    conn = sqlite3.connect(str(path))
    with closing(conn):
        columns = {row[1] for row in conn.execute("PRAGMA table_info(sessions)")}
        pinned = (
            {row[0]: bool(row[1]) for row in conn.execute("SELECT id, pinned FROM sessions")}
            if "pinned" in columns else None
        )
        return columns, pinned


def _homes(tmp_path, monkeypatch, names=("default",)):
    """Give each profile its own home directory; return ``{name: Path}``."""
    import api.profiles as profiles

    homes = {name: tmp_path / name for name in names}
    for home in homes.values():
        home.mkdir(parents=True, exist_ok=True)
    monkeypatch.setattr(
        profiles, "_resolve_profile_home_for_name",
        lambda name: str(homes.get(name or "default", homes["default"])),
    )
    monkeypatch.setattr(profiles, "get_active_hermes_home", lambda: str(homes["default"]))
    monkeypatch.setattr(profiles, "_is_root_profile", lambda name: (name or "default") == "default")
    return homes


class _PinSession:
    """Sidecar session fake: the pin route only needs ``compact()`` and ``save()``."""

    def __init__(self, sid, profile="default"):
        self.session_id, self.profile, self.pinned = sid, profile, False
        self.state_db_mirror = {}

    def compact(self):
        return {"session_id": self.session_id, "pinned": bool(self.pinned), "profile": self.profile}

    def save(self, touch_updated_at=True):
        self.saved = bool(self.pinned)


def _webui_rows(*specs):
    """Sidebar rows as ``all_sessions()`` returns them, from ``(sid, profile, pinned)``."""
    return [
        {"session_id": sid, "profile": profile, "pinned": pinned, "session_source": "webui"}
        for sid, profile, pinned in specs
    ]


def _pin_endpoint(monkeypatch, sessions, *, limit=10):
    """Stub the HTTP envelope of ``/api/session/pin``; return ``post(sid, pinned)``."""
    from api import routes

    state = {}

    def reply(status, payload):
        state["response"] = (status, payload)
        return True

    monkeypatch.setattr(routes, "_get_session_agent_lock", lambda _sid: threading.RLock())
    monkeypatch.setattr(routes, "get_session", lambda sid, **kw: sessions[sid])
    monkeypatch.setattr(routes, "_ensure_full_session_before_mutation", lambda _sid, s: s)
    monkeypatch.setattr(routes, "_session_is_subagent_view_only", lambda _sid: False)
    monkeypatch.setattr(routes, "all_sessions", lambda *a, **kw: [])
    monkeypatch.setattr(routes, "SESSIONS", sessions)  # resident, like a live session
    monkeypatch.setattr(routes, "list_profiles_api", lambda *a, **kw: [{"name": "default", "is_default": True}])
    monkeypatch.setattr(routes, "_root_profile_names_snapshot", lambda: {"default"})
    monkeypatch.setattr(routes, "load_settings", lambda: {"pinned_sessions_limit": limit})
    monkeypatch.setattr(routes, "publish_session_list_changed", lambda *a, **kw: None)
    monkeypatch.setattr(routes, "_guard_request_session_visibility", lambda *a, **kw: True)
    monkeypatch.setattr(routes, "_check_csrf", lambda handler: True)
    monkeypatch.setattr(routes, "read_body", lambda handler: state["body"])
    monkeypatch.setattr(routes, "j", lambda h, payload, status=200, extra_headers=None: reply(status, payload))
    monkeypatch.setattr(routes, "bad", lambda h, msg, status=400: reply(status, {"error": msg}))

    def post(sid, pinned=True):
        state["body"] = {"session_id": sid, "pinned": pinned}
        routes.handle_post(object(), SimpleNamespace(path="/api/session/pin"))
        return state.pop("response")

    return post


def _pin_setup(tmp_path, monkeypatch, **db_kwargs):
    """Homes, a default state.db and a pin endpoint wired to one fake sidecar session."""
    homes = _homes(tmp_path, monkeypatch)
    db = homes["default"] / "state.db"
    _make_state_db(db, **db_kwargs)
    sessions = collections.OrderedDict({"s1": _PinSession("s1")})  # the route re-resolves via the LRU
    return db, sessions, _pin_endpoint(monkeypatch, sessions)


def _sidebar_payload(monkeypatch, rows, *, active_profile="default", all_profiles=False, include_archived=False):
    """Build the sidebar payload over *rows* with listing and pruning stubbed out.

    Returns the row list in payload order, so two profiles keeping the same session id
    stay distinguishable.
    """
    from api import routes

    monkeypatch.setattr(
        routes, "all_sessions",
        lambda **kw: [dict(row, is_cli_session=False, read_only=False, message_count=1) for row in rows],
    )
    monkeypatch.setattr(routes, "_enrich_sidebar_lineage_metadata", lambda _rows: None)
    monkeypatch.setattr(routes, "_reconcile_stale_stream_state_for_session_rows", lambda _rows: False)
    monkeypatch.setattr(routes, "_prune_orphaned_webui_zero_message_sessions", lambda rows, **kw: list(rows))
    monkeypatch.setattr(routes, "get_cli_sessions", lambda *a, **kw: pytest.fail("CLI listing must not run"))
    return routes._build_session_list_cache_payload(
        active_profile=active_profile,
        all_profiles=all_profiles,
        show_cli_sessions=False,
        show_previous_messaging_sessions=False,
        show_cron_sessions=False,
        include_archived=include_archived,
    )["sessions"]


def _sidebar_rows(monkeypatch, rows, **kwargs):
    return {row["session_id"]: row for row in _sidebar_payload(monkeypatch, rows, **kwargs)}


def _pinned_flags(payload_rows):
    """``{(profile, session_id): pinned}`` — keeps same-id rows from two owners apart."""
    return {(row["profile"], row["session_id"]): row["pinned"] for row in payload_rows}


# ── the pin route never fails because of the mirror ──────────────────────────

@pytest.mark.parametrize("db_kwargs,pins", [
    ({"rows": (("s1", 0),), "pinned_column": False}, None),
    ({"rows": ()}, {}),
], ids=["no-pin-column", "no-row"])
def test_pin_and_unpin_save_the_sidecar_when_the_mirror_cannot_land(
    tmp_path, monkeypatch, db_kwargs, pins,
):
    db, sessions, post = _pin_setup(tmp_path, monkeypatch, **db_kwargs)

    assert post("s1", True)[0] == 200
    assert sessions["s1"].pinned is True and sessions["s1"].saved is True
    assert post("s1", False)[0] == 200
    assert sessions["s1"].pinned is False and sessions["s1"].saved is False
    # `pins=None` means the Agent never created the column; {} means it never created the row.
    assert _db_snapshot(db)[1] == pins


def test_pin_and_unpin_save_the_sidecar_while_another_writer_holds_state_db(tmp_path, monkeypatch):
    db, sessions, post = _pin_setup(tmp_path, monkeypatch, rows=(("s1", 0),))

    holder = sqlite3.connect(str(db), isolation_level=None)
    try:
        holder.execute("BEGIN IMMEDIATE")
        started = time.monotonic()
        status, _ = post("s1", True)
        elapsed = time.monotonic() - started
        assert status == 200
        assert sessions["s1"].saved is True
        # Bounded by the short busy timeout (~0.5 s), not unbounded.
        assert elapsed < 3
        assert _db_snapshot(db)[1] == {"s1": False}
        assert post("s1", False)[0] == 200
        assert sessions["s1"].saved is False
    finally:
        holder.execute("ROLLBACK")
        holder.close()


def test_pin_and_unpin_save_the_sidecar_when_the_state_db_row_is_missing(tmp_path, monkeypatch):
    db, sessions, post = _pin_setup(tmp_path, monkeypatch, rows=())

    assert post("s1", True)[0] == 200
    assert sessions["s1"].saved is True
    assert post("s1", False)[0] == 200
    assert sessions["s1"].saved is False
    # Nothing invents the row the Agent does not have.
    assert _db_snapshot(db)[1] == {}


def test_pin_mirrors_to_state_db_and_unpin_clears_it(tmp_path, monkeypatch):
    db, _sessions, post = _pin_setup(tmp_path, monkeypatch, rows=(("s1", 0), ("other", 0)))

    assert post("s1", True)[0] == 200
    assert _db_snapshot(db)[1] == {"s1": True, "other": False}
    assert post("s1", False)[0] == 200
    assert _db_snapshot(db)[1] == {"s1": False, "other": False}


# ── the sidebar reads state.db pins as an extra input ────────────────────────

def test_desktop_pin_shows_in_the_sidebar(tmp_path, monkeypatch):
    homes = _homes(tmp_path, monkeypatch)
    _make_state_db(homes["default"] / "state.db", rows=(("s1", 1), ("s2", 0)))

    rows = _sidebar_rows(monkeypatch, _webui_rows(("s1", "default", False), ("s2", "default", False)))

    assert rows["s1"]["pinned"] is True
    assert rows["s2"]["pinned"] is False


def test_webui_pin_survives_a_mirror_that_did_not_land(tmp_path, monkeypatch):
    homes = _homes(tmp_path, monkeypatch)
    # state.db still says unpinned: the mirror lost the writer when the user pinned.
    _make_state_db(homes["default"] / "state.db", rows=(("s1", 0),))

    rows = _sidebar_rows(monkeypatch, _webui_rows(("s1", "default", True)))

    assert rows["s1"]["pinned"] is True


def test_sidebar_build_never_writes_state_db(tmp_path, monkeypatch):
    homes = _homes(tmp_path, monkeypatch)
    db = homes["default"] / "state.db"
    _make_state_db(db, rows=(("s1", 1), ("s2", 0)))
    before = (db.read_bytes(), _db_snapshot(db))

    rows = _sidebar_rows(monkeypatch, _webui_rows(("s1", "default", False), ("s2", "default", True)))

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
    real_read = state_sync.read_state_db_flags

    def spy(profile=None):
        opened.append(str(state_sync._resolve_state_db_path(profile)))
        return real_read(profile)

    monkeypatch.setattr(state_sync, "read_state_db_flags", spy)

    rows = _sidebar_rows(monkeypatch, _webui_rows(("s1", "default", False), ("s2", "default", False)))

    assert opened == [str(db_a)]
    assert {sid: row["pinned"] for sid, row in rows.items()} == {"s1": False, "s2": True}
    assert (db_b.read_bytes(), _db_snapshot(db_b)) == before_b


def test_all_profiles_marks_only_each_owners_own_pin(tmp_path, monkeypatch):
    """A copied/restored history id in two profiles must not borrow the other's pin."""
    homes = _homes(tmp_path, monkeypatch, names=("default", "work"))
    # A/default keeps s1 unpinned; B/work pins its own s1 and one other session.
    _make_state_db(homes["default"] / "state.db", rows=(("s1", 0),))
    _make_state_db(homes["work"] / "state.db", rows=(("s1", 1), ("b_other", 0), ("b_pinned", 1)))

    rows = _pinned_flags(_sidebar_payload(monkeypatch, _webui_rows(
        ("s1", "default", False), ("s1", "work", False),
        ("b_other", "work", False), ("b_pinned", "work", False),
    ), all_profiles=True))

    assert rows == {
        ("default", "s1"): False,
        ("work", "s1"): True,
        ("work", "b_other"): False,
        ("work", "b_pinned"): True,
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

    rows = _pinned_flags(_sidebar_payload(monkeypatch, _webui_rows(
        ("s1", "default", False), ("root_pinned", "kinni", False), ("w_pinned", "work", False),
    ), all_profiles=True))

    assert rows == {
        ("default", "s1"): False,
        ("kinni", "root_pinned"): True,
        ("work", "w_pinned"): True,
    }


def test_agent_projection_carries_the_state_db_pin(tmp_path, monkeypatch):
    from api import models

    db = tmp_path / "state.db"
    with closing(sqlite3.connect(str(db))) as conn:
        conn.executescript(
            "CREATE TABLE sessions (id TEXT PRIMARY KEY, source TEXT, title TEXT, model TEXT,"
            " started_at REAL NOT NULL, message_count INTEGER DEFAULT 0,"
            " pinned INTEGER NOT NULL DEFAULT 0);"
            " CREATE TABLE messages (id TEXT PRIMARY KEY, session_id TEXT, role TEXT,"
            " content TEXT, timestamp REAL);"
        )
        sessions = (("cli_pinned", 1), ("cli_plain", 0))
        conn.executemany(
            "INSERT INTO sessions (id, source, title, model, started_at, message_count, pinned)"
            " VALUES (?, 'cli', ?, 'openai/gpt-5', 1.0, 2, ?)",
            [(sid, sid, pinned) for sid, pinned in sessions],
        )
        conn.executemany(
            "INSERT INTO messages (id, session_id, role, content, timestamp)"
            " VALUES (?, ?, 'user', 'hi', 1.5)",
            [(f"m_{sid}", sid) for sid, _pinned in sessions],
        )
        conn.commit()

    monkeypatch.setattr(models, "get_last_workspace", lambda *_a, **_kw: str(tmp_path))
    monkeypatch.setattr(models, "ensure_cron_project", lambda *_a, **_kw: None, raising=False)
    monkeypatch.setattr(models, "ensure_webhook_project", lambda *_a, **_kw: None, raising=False)
    monkeypatch.setattr(models, "_profile_has_user_projects", lambda *_a, **_kw: False, raising=False)

    rows = {row["session_id"]: row for row in models._load_cli_sessions_uncached(tmp_path, db, None)}

    assert rows["cli_pinned"]["pinned"] is True
    assert rows["cli_plain"]["pinned"] is False


# ── Desktop/CLI unpin and archive reach the sidebar; archive and title are mirrored ──

def _flag_row(sid, **extra):
    return {**_webui_rows((sid, "default", False))[0], **extra}


def test_desktop_unpin_after_a_webui_mirror_is_honoured(tmp_path, monkeypatch):
    homes = _homes(tmp_path, monkeypatch)
    _make_state_db(homes["default"] / "state.db", rows=(("s1", 0),))
    row = _flag_row("s1", pinned=True, state_db_mirror={"pinned": True})

    assert _sidebar_rows(monkeypatch, [row])["s1"]["pinned"] is False


@pytest.mark.parametrize("mirror", [{}, {"pinned": False}], ids=["never-mirrored", "last-mirrored-unpinned"])
def test_a_webui_pin_the_mirror_missed_is_not_unpinned(tmp_path, monkeypatch, mirror):
    homes = _homes(tmp_path, monkeypatch)
    _make_state_db(homes["default"] / "state.db", rows=(("s1", 0),))
    row = _flag_row("s1", pinned=True, state_db_mirror=mirror)

    assert _sidebar_rows(monkeypatch, [row])["s1"]["pinned"] is True


def test_pin_route_records_only_a_mirror_that_landed(tmp_path, monkeypatch):
    db, sessions, post = _pin_setup(tmp_path, monkeypatch, rows=(("s1", 0),))
    assert post("s1", True)[0] == 200
    assert sessions["s1"].state_db_mirror == {"pinned": True}

    holder = sqlite3.connect(str(db), isolation_level=None)
    try:
        holder.execute("BEGIN IMMEDIATE")
        assert post("s1", False)[0] == 200
    finally:
        holder.execute("ROLLBACK")
        holder.close()
    assert sessions["s1"].state_db_mirror == {"pinned": True}  # the unpin never landed


def test_desktop_archive_and_unarchive_reach_the_sidebar(tmp_path, monkeypatch):
    homes = _homes(tmp_path, monkeypatch)
    _make_state_db(homes["default"] / "state.db", rows=(("arch", 0), ("unarch", 0)))
    with closing(sqlite3.connect(str(homes["default"] / "state.db"))) as conn:
        conn.execute("UPDATE sessions SET archived = 1 WHERE id = 'arch'")
        conn.commit()
    rows = [
        _flag_row("arch", archived=False),
        _flag_row("unarch", archived=True, state_db_mirror={"archived": True}),
    ]

    got = {r["session_id"]: r["archived"] for r in _sidebar_payload(monkeypatch, rows, include_archived=True)}

    assert got == {"arch": True, "unarch": False}


def test_archive_mirrors_across_the_compression_lineage_and_title_mirrors_as_user_title(tmp_path, monkeypatch):
    from api import routes

    homes = _homes(tmp_path, monkeypatch)
    db = homes["default"] / "state.db"
    _make_state_db(db, rows=(("root", 0), ("tip", 0), ("other", 0)))
    with closing(sqlite3.connect(str(db))) as conn:
        conn.execute("UPDATE sessions SET end_reason = 'compression' WHERE id = 'root'")
        conn.execute("UPDATE sessions SET parent_session_id = 'root' WHERE id = 'tip'")
        conn.commit()
    monkeypatch.setattr(routes, "_get_session_agent_lock", lambda _sid: threading.RLock())
    tip = _PinSession("tip")
    monkeypatch.setattr(routes, "SESSIONS", {"tip": tip})

    routes._mirror_to_state_db(tip, "archived", True)
    routes._mirror_to_state_db(tip, "title", "Manual name")

    with closing(sqlite3.connect(str(db))) as conn:
        archived = dict(conn.execute("SELECT id, archived FROM sessions"))
        title = conn.execute("SELECT title, title_source FROM sessions WHERE id = 'tip'").fetchone()
    assert archived == {"root": 1, "tip": 1, "other": 0}
    assert title == ("Manual name", "user")
    assert tip.state_db_mirror == {"archived": True, "title": "Manual name"}


def test_title_clash_or_missing_row_never_raises_and_is_not_recorded(tmp_path, monkeypatch):
    from api import routes

    homes = _homes(tmp_path, monkeypatch)
    db = homes["default"] / "state.db"
    _make_state_db(db, rows=(("s1", 0),))
    monkeypatch.setattr(routes, "_get_session_agent_lock", lambda _sid: threading.RLock())
    with closing(sqlite3.connect(str(db))) as conn:
        conn.execute("CREATE UNIQUE INDEX t ON sessions(title) WHERE title IS NOT NULL")
        conn.execute("INSERT INTO sessions (id, title) VALUES ('s2', 'Taken')")
        conn.commit()
    for sid, title in (("s1", "Taken"), ("ghost", "Anything")):
        session = _PinSession(sid)
        routes._mirror_to_state_db(session, "title", title)
        assert session.state_db_mirror == {}


def _real_session_pin_endpoint(tmp_path, monkeypatch, sid="s1"):
    """Pin route over REAL Session persistence, the real SESSIONS LRU and per-session lock."""
    import api.models as models
    from api import routes
    from api.models import Session

    homes = _homes(tmp_path, monkeypatch)
    _make_state_db(homes["default"] / "state.db", rows=((sid, 0),))
    sdir = tmp_path / "sessions"
    sdir.mkdir()
    for mod in (models, routes):
        monkeypatch.setattr(mod, "SESSION_DIR", sdir, raising=False)
    monkeypatch.setattr(models, "SESSION_INDEX_FILE", sdir / "_index.json", raising=False)
    monkeypatch.setattr(models, "SESSIONS_MAX", 1)
    monkeypatch.setattr(models._cfg, "get_sessions_cache_max", lambda *a, **k: 1)
    live = Session(session_id=sid, title="Old title", messages=[{"role": "user", "content": "one"}])
    live.save()
    cache = collections.OrderedDict({sid: live})
    monkeypatch.setattr(models, "SESSIONS", cache)
    monkeypatch.setattr(routes, "SESSIONS", cache)
    post = _pin_endpoint(monkeypatch, cache)  # the route must see the same LRU the test mutates
    from api.config import _get_session_agent_lock as real_lock
    monkeypatch.setattr(routes, "_get_session_agent_lock", real_lock)
    monkeypatch.setattr(routes, "get_session", lambda s, **kw: cache[s])
    return live, cache, sdir, post, Session


def test_mirror_ack_after_eviction_and_cold_writer_keeps_new_title_and_transcript(tmp_path, monkeypatch):
    import json
    import api.models as models

    live, cache, sdir, post, Session = _real_session_pin_endpoint(tmp_path, monkeypatch)
    real_sync = __import__("api.state_sync", fromlist=["x"]).sync_session_field

    def sync_then_cold_write(*args, **kwargs):
        landed = real_sync(*args, **kwargs)  # the state.db mirror completes ...
        with models.LOCK:  # ... real cap-1 LRU eviction of the resident instance ...
            cache["other"] = Session(session_id="other", title="x", messages=[])
            models._evict_sessions_over_cap(1)
        assert "s1" not in cache
        cold = Session.load("s1")  # ... a distinct cold instance saves newer state
        cold.title, cold.manual_title = "New manual title", True
        cold.messages.append({"role": "assistant", "content": "two"})
        cold.save()
        cache["s1"] = cold
        return landed

    monkeypatch.setattr("api.state_sync.sync_session_field", sync_then_cold_write)
    assert post("s1", True)[0] == 200

    data = json.loads((sdir / "s1.json").read_text())
    assert data["title"] == "New manual title"
    assert [m["content"] for m in data["messages"]] == ["one", "two"]
    assert data["pinned"] is True and data["state_db_mirror"] == {"pinned": True}


def test_mirror_ack_save_failure_still_returns_200_with_both_stores_pinned(tmp_path, monkeypatch):
    import json
    from api.models import Session

    live, cache, sdir, post, _ = _real_session_pin_endpoint(tmp_path, monkeypatch)
    real_save = Session.save
    calls = []

    def save(self, touch_updated_at=True):
        calls.append(touch_updated_at)
        if touch_updated_at is False:
            raise PermissionError("sidecar dir is read-only")
        return real_save(self, touch_updated_at)

    monkeypatch.setattr(Session, "save", save)
    assert post("s1", True)[0] == 200
    assert False in calls  # the acknowledgement save was attempted and failed
    assert json.loads((sdir / "s1.json").read_text())["pinned"] is True
    db = tmp_path / "default" / "state.db"
    assert _db_snapshot(db)[1] == {"s1": True}


# ── review 5670121894: delete, lineage boundaries, stale mirrors ────────────

def _lineage_db(path):
    """root(compressed) -> tip (continuation), fork (_branched_from), tool child."""
    path.parent.mkdir(parents=True, exist_ok=True)
    with closing(sqlite3.connect(str(path))) as conn:
        conn.execute(
            "CREATE TABLE sessions (id TEXT PRIMARY KEY, pinned INTEGER NOT NULL DEFAULT 0,"
            " archived INTEGER NOT NULL DEFAULT 0, auto_archived INTEGER NOT NULL DEFAULT 0,"
            " title TEXT, title_source TEXT, parent_session_id TEXT, end_reason TEXT,"
            " model_config TEXT, source TEXT)"
        )
        conn.executemany(
            "INSERT INTO sessions (id, parent_session_id, end_reason, model_config, source) VALUES (?,?,?,?,?)",
            [
                ("root", None, "compression", None, "webui"),
                ("tip", "root", None, None, "webui"),
                ("fork", "root", None, '{"_branched_from": "root"}', "webui"),
                ("tool", "root", None, None, "tool"),
            ],
        )
        conn.commit()


def _column(path, column):
    with closing(sqlite3.connect(str(path))) as conn:
        return dict(conn.execute(f"SELECT id, {column} FROM sessions"))


@pytest.mark.parametrize("field,column", [("archived", "archived"), ("pinned", "pinned")])
def test_lineage_mirror_follows_compression_but_not_forks_or_tool_children(tmp_path, monkeypatch, field, column):
    from api.state_sync import sync_session_field

    homes = _homes(tmp_path, monkeypatch)
    db = homes["default"] / "state.db"
    _lineage_db(db)

    assert sync_session_field("root", field, True)  # root-issued
    assert _column(db, column) == {"root": 1, "tip": 1, "fork": 0, "tool": 0}
    assert sync_session_field("root", field, False)
    assert sync_session_field("fork", field, True)  # fork-issued never reaches its parent
    assert _column(db, column) == {"root": 0, "tip": 0, "fork": 1, "tool": 0}


def test_pin_and_unpin_of_the_tip_reach_the_whole_compression_lineage(tmp_path, monkeypatch):
    from api.state_sync import sync_session_field

    homes = _homes(tmp_path, monkeypatch)
    db = homes["default"] / "state.db"
    _lineage_db(db)

    assert sync_session_field("tip", "pinned", True)
    assert _column(db, "pinned") == {"root": 1, "tip": 1, "fork": 0, "tool": 0}
    assert sync_session_field("tip", "pinned", False)
    assert _column(db, "pinned") == {"root": 0, "tip": 0, "fork": 0, "tool": 0}


def test_completed_delete_is_not_undone_by_the_mirror_acknowledgement(tmp_path, monkeypatch):
    import api.state_sync as state_sync

    live, cache, sdir, post, _ = _real_session_pin_endpoint(tmp_path, monkeypatch)
    real_sync = state_sync.sync_session_field

    def sync_then_delete(*args, **kwargs):
        landed = real_sync(*args, **kwargs)
        cache.pop("s1", None)  # the real delete endpoint finished: cache entry and sidecar gone
        (sdir / "s1.json").unlink()
        return landed

    monkeypatch.setattr(state_sync, "sync_session_field", sync_then_delete)
    assert post("s1", True)[0] == 200
    assert not (sdir / "s1.json").exists()
    assert "s1" not in cache


@pytest.mark.parametrize("sidecar_pinned,stale_value", [(False, True), (True, False)], ids=["pin-then-unpin", "unpin-then-pin"])
def test_superseded_mirror_never_reaches_state_db(tmp_path, monkeypatch, sidecar_pinned, stale_value):
    from api import routes

    homes = _homes(tmp_path, monkeypatch)
    db = homes["default"] / "state.db"
    _make_state_db(db, rows=(("s1", int(sidecar_pinned)),))  # the newer action already mirrored
    session = _PinSession("s1")
    session.pinned = sidecar_pinned
    monkeypatch.setattr(routes, "SESSIONS", {"s1": session})
    monkeypatch.setattr(routes, "_ensure_full_session_before_mutation", lambda _sid, s: s)
    monkeypatch.setattr(routes, "_get_session_agent_lock", lambda _sid: threading.RLock())

    routes._mirror_to_state_db(session, "pinned", stale_value)  # the older action's mirror arrives late

    assert _db_snapshot(db)[1] == {"s1": sidecar_pinned}
    assert session.state_db_mirror == {}
