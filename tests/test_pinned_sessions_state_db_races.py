"""Review follow-ups: a late manual-rename mirror after a reset, and id-scoped flag reads."""

import sqlite3
import threading
from contextlib import closing
import collections

import pytest

from tests.test_pinned_sessions_state_db_mirror import _PinSession, _column, _homes, _make_state_db, _sidebar_rows


def test_late_rename_mirror_cannot_undo_a_mirrored_reset(tmp_path, monkeypatch):
    from api import routes
    from api.session_ops import apply_session_title_rename

    db = _homes(tmp_path, monkeypatch)["default"] / "state.db"
    _make_state_db(db, rows=(("s1", 0),))
    monkeypatch.setattr(routes, "_get_session_agent_lock", lambda _sid: threading.RLock())
    session = _PinSession("s1")
    monkeypatch.setattr(routes, "SESSIONS", {"s1": session})
    apply_session_title_rename(session, "Old manual name")
    apply_session_title_rename(session, "Untitled")  # the reset lands and mirrors first
    routes._mirror_session_title(session)

    routes._mirror_to_state_db(session, "title", "Old manual name")  # the rename's mirror arrives late

    with closing(sqlite3.connect(str(db))) as conn:
        assert conn.execute("SELECT title, title_source FROM sessions WHERE id = 's1'").fetchone() == (None, None)


def test_flag_reader_fetches_only_listed_ids_in_bounded_batches(tmp_path, monkeypatch):
    from api import state_sync

    db = _homes(tmp_path, monkeypatch)["default"] / "state.db"
    _make_state_db(db, rows=[(f"s{i}", i % 2) for i in range(3000)])
    wanted = {f"s{i}" for i in range(0, 2400, 2)} | {"missing"}
    queries = []
    real_open = state_sync.open_state_db_readonly

    def tracing_open(path):
        conn = real_open(path)
        conn.set_trace_callback(queries.append)
        return conn

    monkeypatch.setattr(state_sync, "open_state_db_readonly", tracing_open)
    flags = state_sync.read_state_db_flags("default", wanted)

    assert set(flags) == wanted - {"missing"}
    assert all(f == {"pinned": False, "archived": False} for f in flags.values())
    selects = [q for q in queries if q.startswith("SELECT id")]
    assert len(selects) == 3 and all("WHERE id IN" in q for q in selects)
    assert state_sync.read_state_db_flags("default", ()) == {}


def test_flag_reader_without_flag_columns_returns_nothing(tmp_path, monkeypatch):
    from api import state_sync

    db = _homes(tmp_path, monkeypatch)["default"] / "state.db"
    _make_state_db(db, rows=(("s1", 0),), pinned_column=False)
    assert state_sync.read_state_db_flags("default", {"s1"}) == {}


def _two_id_lineage(tmp_path, monkeypatch, root_pinned):
    """Real Session sidecars for a compressed root and its tip, both resident, plus the lineage state.db."""
    import api.models as models
    from api import routes
    from api.models import Session
    from tests.test_pinned_sessions_state_db_mirror import _lineage_db, _pin_endpoint

    db = _homes(tmp_path, monkeypatch)["default"] / "state.db"
    _lineage_db(db)
    with closing(sqlite3.connect(str(db))) as conn:
        conn.execute("UPDATE sessions SET pinned = ? WHERE id IN ('root', 'tip')", (int(root_pinned),))
        conn.commit()
    sdir = tmp_path / "sessions"
    sdir.mkdir()
    for mod in (models, routes):
        monkeypatch.setattr(mod, "SESSION_DIR", sdir, raising=False)
    monkeypatch.setattr(models, "SESSION_INDEX_FILE", sdir / "_index.json", raising=False)
    cache = collections.OrderedDict()
    for sid in ("root", "tip"):
        cache[sid] = Session(session_id=sid, title=sid, messages=[{"role": "user", "content": sid}], pinned=root_pinned)
        cache[sid].save()
    monkeypatch.setattr(models, "SESSIONS", cache)
    post = _pin_endpoint(monkeypatch, cache)
    from api.config import _get_session_agent_lock as real_lock
    monkeypatch.setattr(routes, "_get_session_agent_lock", real_lock)
    monkeypatch.setattr(routes, "get_session", lambda s, **kw: cache[s])
    return db, cache, post


@pytest.mark.parametrize("old_root_value", [True, False], ids=["root-pin-vs-newer-tip-unpin", "root-unpin-vs-newer-tip-pin"])
def test_older_root_mirror_resuming_after_newer_tip_action_keeps_the_tip_value(tmp_path, monkeypatch, old_root_value):
    import json
    from api import routes

    db, cache, post = _two_id_lineage(tmp_path, monkeypatch, root_pinned=not old_root_value)
    real_mirror = routes._mirror_to_state_db
    tip_reply = {}

    def root_pauses_after_its_save(session, field, value, *args, **kwargs):
        if session.session_id == "root":  # A saved its sidecar; B runs to completion before A mirrors
            tip_reply["r"] = post("tip", not old_root_value)
        return real_mirror(session, field, value, *args, **kwargs)

    monkeypatch.setattr(routes, "_mirror_to_state_db", root_pauses_after_its_save)
    assert post("root", old_root_value)[0] == 200
    assert tip_reply["r"][0] == 200

    sdir = tmp_path / "sessions"
    assert json.loads((sdir / "tip.json").read_text())["pinned"] is (not old_root_value)
    assert json.loads((sdir / "root.json").read_text())["pinned"] is old_root_value
    pins = _column(db, "pinned")
    assert (pins["root"], pins["tip"]) == (int(not old_root_value),) * 2  # the newer tip action owns the lineage
    rows = [{"session_id": sid, "profile": "default", "session_source": "webui",
             **{k: json.loads((sdir / f"{sid}.json").read_text())[k] for k in ("pinned", "state_db_mirror")}}
            for sid in ("tip",)]
    assert _sidebar_rows(monkeypatch, rows)["tip"]["pinned"] is (not old_root_value)


def test_busy_state_db_of_one_profile_does_not_stall_other_profiles(tmp_path, monkeypatch):
    import time
    from api import routes, state_sync
    from tests.test_pinned_sessions_state_db_mirror import _pin_endpoint

    homes = _homes(tmp_path, monkeypatch, names=("default", "alpha"))
    _make_state_db(homes["alpha"] / "state.db", rows=(("a1", 0),))
    _make_state_db(homes["default"] / "state.db", rows=(("b1", 0),))
    a1, b1 = _PinSession("a1", profile="alpha"), _PinSession("b1")
    post = _pin_endpoint(monkeypatch, collections.OrderedDict({"a1": a1, "b1": b1}))
    blocker = sqlite3.connect(str(homes["alpha"] / "state.db"), isolation_level=None)
    blocker.execute("BEGIN EXCLUSIVE")  # another client holds alpha's database
    try:
        a1.pinned = True
        a1.save()  # the sidecar is saved before the mirror starts
        timing = {}

        def mirror_a():
            start = time.monotonic()
            routes._mirror_to_state_db(a1, "pinned", True)
            timing["a"] = time.monotonic() - start

        thread = threading.Thread(target=mirror_a)
        thread.start()
        time.sleep(0.1)  # alpha's mirror is now blocked on the busy database
        start = time.monotonic()
        routes._mirror_ticket()
        assert post("b1", True)[0] == 200
        elapsed = time.monotonic() - start
        thread.join(5)
    finally:
        blocker.rollback()
        blocker.close()
    assert elapsed < state_sync.PIN_MIRROR_TIMEOUT_S  # profile B never waited behind alpha
    assert _column(homes["default"] / "state.db", "pinned")["b1"] == 1
    assert timing["a"] < 3 * state_sync.PIN_MIRROR_TIMEOUT_S  # alpha's mirror gave up, bounded
    assert a1.saved is True and _column(homes["alpha"] / "state.db", "pinned")["a1"] == 0
