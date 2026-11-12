"""#9017: reads never save an older copy over a newer turn.

Two read paths could overwrite a turn the owner had just saved under the session lock:

- ``Session.load``'s duplicate-partial repair: a full load that collapses adjacent duplicate
  partials saved the copy it read, without the session lock. A sidebar poll from any profile
  reaches it (the stale-stream cleanup's reload; the listing's metadata refresh, through
  ``load_metadata_only()``'s fallback for older layouts).
- ``_clear_stale_stream_state``: handed a metadata-only stub naming a dead stream, it reloads the
  full session (#1558), then takes the lock, clears the stream and saves. A turn saved between the
  reload and the lock was overwritten by that older copy.

The repair save now runs only under the session lock (taken without waiting) and only when nobody
wrote the file since the read; otherwise the collapse stays in memory and a later save persists it.
The cleanup still reloads outside the lock, so the reload's own self-heals (the state.db sync, the
pending repair) keep working, and under the lock it skips this time if another writer saved since.
"""

import io
import json
import sqlite3
import threading
import time
import traceback
from collections import OrderedDict
from urllib.parse import urlparse

import pytest

SID = "stale-9017"
ANCHOR = "anchor-9017"
PARTIAL = {"role": "assistant", "content": "same", "_partial": True}
POLL_THREAD = "foreign-sidebar-poll-9017"
NOW = time.time()


@pytest.fixture
def store(tmp_path, monkeypatch):
    import api.config as config
    import api.models as models
    import api.routes as routes

    sessions = OrderedDict()
    directory = tmp_path / "sessions"
    directory.mkdir()
    home = tmp_path / "home"
    (home / "workspace").mkdir(parents=True)
    for module in (config, models, routes):
        if hasattr(module, "SESSION_DIR"):
            monkeypatch.setattr(module, "SESSION_DIR", directory)
        if hasattr(module, "SESSIONS"):
            monkeypatch.setattr(module, "SESSIONS", sessions)
        if hasattr(module, "SESSION_INDEX_FILE"):
            monkeypatch.setattr(module, "SESSION_INDEX_FILE", directory / "_index.json")
    monkeypatch.setattr(models, "_LEGACY_SIDECAR_FACTS", OrderedDict())
    monkeypatch.setattr(models, "_get_profile_home", lambda profile: home / "profiles" / str(profile or "default"))
    monkeypatch.setattr(models, "_active_state_db_path", lambda: home / "state.db")
    if hasattr(routes, "_active_state_db_path"):
        monkeypatch.setattr(routes, "_active_state_db_path", lambda: home / "state.db")
    monkeypatch.setattr(models, "_start_session_index_rebuild_thread", lambda: None)
    monkeypatch.setattr(routes, "load_settings", lambda: {"show_cli_sessions": False})
    routes._session_list_cache_clear()
    yield directory, home, sessions
    routes._session_list_cache_clear()
    config.STREAMS.pop("new-live-stream", None)


def _messages(partials):
    messages = [{"role": "user", "content": "hello", "timestamp": NOW - 60}]
    if partials:
        return messages + [dict(PARTIAL), dict(PARTIAL)]
    return messages + [{"role": "assistant", "content": "hi", "timestamp": NOW - 58}]


def _write(store, layout, *, partials):
    """`work`'s session, whose metadata prefix names a dead stream.

    ``webui`` is the writer's layout (``profile`` before ``messages``), ``outside`` puts ``profile``
    after them; both carry ``message_count``, so a metadata read is a prefix stub. ``legacy`` is an
    older WebUI layout without ``message_count``/``anchor_scene_index``: the listing's metadata
    refresh reads it through ``load_metadata_only()``'s full-load fallback.
    """
    directory, home, _sessions = store
    messages = _messages(partials)
    body = {
        "session_id": SID, "title": "Private", "created_at": NOW - 60, "updated_at": NOW - 59,
        "workspace": str(home / "workspace"), "source_tag": "webui", "active_stream_id": "dead-stream",
    }
    if layout != "legacy":
        body.update(message_count=len(messages), anchor_scene_index={})
    if layout in ("webui", "legacy"):
        body["profile"] = "work"
    body["messages"] = messages
    if layout == "outside":
        body["profile"] = "work"
    path = directory / f"{SID}.json"
    path.write_text(json.dumps(body), encoding="utf-8")
    anchor = {"session_id": ANCHOR, "title": "Anchor", "profile": "default", "created_at": NOW - 30,
              "updated_at": NOW - 30, "message_count": 1}
    (directory / f"{ANCHOR}.json").write_text(
        json.dumps(dict(anchor, anchor_scene_index={}, messages=[{"role": "user", "content": "a", "timestamp": NOW - 30}])),
        encoding="utf-8",
    )
    row = {"session_id": SID, "title": "Private", "profile": "work", "created_at": NOW - 60,
           "updated_at": NOW - 59, "message_count": len(messages), "last_message_at": NOW - 58,
           "active_stream_id": "dead-stream"}
    (directory / "_index.json").write_text(json.dumps([dict(anchor, last_message_at=NOW - 30), row]), encoding="utf-8")
    return path


class _Handler:
    def __init__(self):
        self.status = None
        self.wfile = io.BytesIO()
        self.rfile = io.BytesIO()
        self.headers = {"Content-Length": "0"}
        self.request = None
        self.client_address = ("127.0.0.1", 1)

    def send_response(self, status):
        self.status = status

    def send_header(self, *args):
        pass

    def end_headers(self):
        pass


def _in_cleanup():
    return any(frame.name == "_clear_stale_stream_state" for frame in traceback.extract_stack())


def _race_foreign_poll(store, monkeypatch, *, pause_at):
    """Pause ``default``'s ``/api/sessions`` poll on SID, then let the owner checkpoint a turn.

    ``pause_at="save"`` pauses at the poll's first save of SID (a read's repair save);
    ``pause_at="load"`` pauses right after the stale-stream cleanup's reload (``get_session``)
    returns. The owner then saves a new turn under the session lock: at once if the poll doesn't
    hold the lock, otherwise once it's released. Returns the file as it ends up, and what the poll saw.
    """
    import api.config as config
    import api.models as models
    import api.profiles as profiles
    import api.routes as routes

    directory, _home, sessions = store
    lock = config._get_session_agent_lock(SID)
    entered, resume = threading.Event(), threading.Event()
    seen = {"status": None, "errors": []}
    real_save = models.Session.save
    real_load = models.Session.load.__func__
    real_get_session = models.get_session

    def _pause():
        if threading.current_thread().name == POLL_THREAD and not entered.is_set():
            entered.set()
            resume.wait(10)

    def _save_spy(self, *args, **kwargs):
        if self.session_id == SID and pause_at == "save":
            _pause()
        return real_save(self, *args, **kwargs)

    def _get_session_spy(sid, *args, **kwargs):
        loaded = real_get_session(sid, *args, **kwargs)
        if sid == SID and pause_at == "load" and not kwargs.get("metadata_only", False) and _in_cleanup():
            _pause()
        return loaded

    monkeypatch.setattr(models.Session, "save", _save_spy)
    monkeypatch.setattr(models, "get_session", _get_session_spy)

    def _poll():
        routes._session_list_cache_clear()
        handler = _Handler()
        profiles.set_request_profile("default")
        try:
            routes.handle_get(handler, urlparse("/api/sessions"))
            seen["status"] = handler.status
        except Exception as exc:  # noqa: BLE001 - reported by the caller
            seen["errors"].append(repr(exc))
        finally:
            profiles.clear_request_profile()

    poll = threading.Thread(target=_poll, name=POLL_THREAD)
    poll.start()
    deadline = time.monotonic() + 10
    while not entered.is_set() and poll.is_alive() and time.monotonic() < deadline:
        time.sleep(0.002)
    assert entered.is_set(), "the poll never reached the pause point"

    def _owner_turn():
        current = real_load(models.Session, SID)
        current.active_stream_id = "new-live-stream"
        current.pending_user_message = "accepted next prompt"
        current.pending_started_at = time.time()
        current.messages.append({"role": "user", "content": "accepted next prompt", "timestamp": current.pending_started_at})
        with config.LOCK:
            sessions[SID] = current
        config.STREAMS["new-live-stream"] = config.create_stream_channel()
        real_save(current, touch_updated_at=False)

    if lock.acquire(timeout=0.5):
        try:
            _owner_turn()
        finally:
            lock.release()
        resume.set()
    else:
        resume.set()
        with lock:
            _owner_turn()
    poll.join(20)
    assert not poll.is_alive()
    return json.loads((directory / f"{SID}.json").read_text(encoding="utf-8")), seen


def _assert_turn_kept(after, seen):
    assert seen["errors"] == [] and seen["status"] == 200, seen
    assert any(m.get("content") == "accepted next prompt" for m in after["messages"]), after["messages"]
    assert after.get("pending_user_message") == "accepted next prompt"
    assert after.get("active_stream_id") == "new-live-stream"


@pytest.mark.parametrize("layout", ["webui", "outside", "legacy"])
def test_foreign_poll_repair_save_never_overwrites_a_newer_turn(store, monkeypatch, layout):
    """A poll's repair save (the cleanup's reload, or the listing refresh for the legacy layout)
    racing the owner's checkpoint: the owner's turn is kept."""
    _write(store, layout, partials=True)
    after, seen = _race_foreign_poll(store, monkeypatch, pause_at="save")
    _assert_turn_kept(after, seen)


@pytest.mark.parametrize("layout", ["webui", "outside"])
def test_turn_saved_between_reload_and_clear_is_kept(store, monkeypatch, layout):
    """No repair save: a writer that lands after the cleanup's reload must not be overwritten
    by the cleanup clearing its older copy."""
    _write(store, layout, partials=False)
    after, seen = _race_foreign_poll(store, monkeypatch, pause_at="load")
    _assert_turn_kept(after, seen)


def test_cleanup_keeps_a_reply_only_state_db_has(store, monkeypatch):
    """The reload's state.db sync still runs (it takes the session lock itself): a reply the
    worker wrote to state.db but not to the sidecar is recovered, not replaced by an
    interruption marker."""
    import api.models as models
    import api.routes as routes

    directory, home, _sessions = store
    monkeypatch.setattr(models, "_get_profile_home", lambda profile: home)
    start = time.time() - 120
    old = [{"role": "user", "content": "old question", "timestamp": start},
           {"role": "assistant", "content": "old answer", "timestamp": start + 1}]
    state = old + [{"role": "user", "content": "new request", "timestamp": start + 2},
                   {"role": "assistant", "content": "reply only in state db", "timestamp": start + 3}]
    with sqlite3.connect(home / "state.db") as conn:
        conn.execute("CREATE TABLE messages (id INTEGER PRIMARY KEY, session_id TEXT, role TEXT, content TEXT, timestamp REAL)")
        conn.executemany("INSERT INTO messages(session_id, role, content, timestamp) VALUES (?, ?, ?, ?)",
                         [(SID, m["role"], m["content"], m["timestamp"]) for m in state])
    models.Session(session_id=SID, title="sync", profile="default", messages=old, context_messages=old,
                   active_stream_id="dead-sync", pending_user_message="new request",
                   pending_started_at=start + 2).save()
    _sessions.clear()
    stub = models.get_session(SID, metadata_only=True)
    assert stub._loaded_metadata_only
    routes._clear_stale_stream_state(stub)
    on_disk = models.Session.load(SID)
    assert any(m.get("content") == "reply only in state db" for m in on_disk.messages)
    assert not any(m.get("_error") for m in on_disk.messages)
    assert on_disk.active_stream_id is None


def test_held_lock_skips_without_a_reload(store, monkeypatch):
    """Observation never queues behind a writer: with the lock held elsewhere, the cleanup
    returns at once, loads nothing and writes nothing."""
    import api.config as config
    import api.models as models
    import api.routes as routes

    path = _write(store, "webui", partials=True)
    before = path.read_bytes()
    stub = models.Session.load_metadata_only(SID)
    assert getattr(stub, "_loaded_metadata_only", False) and stub.active_stream_id == "dead-stream"
    loads = []
    real_load = models.Session.load.__func__
    monkeypatch.setattr(models.Session, "load", classmethod(lambda cls, sid, *a, **k: loads.append(sid) or real_load(cls, sid, *a, **k)))
    result = {}
    lock = config._get_session_agent_lock(SID)
    with lock:
        worker = threading.Thread(target=lambda: result.setdefault("cleared", routes._clear_stale_stream_state(stub)))
        worker.start()
        worker.join(5)
        assert not worker.is_alive(), "the cleanup queued behind the writer"
    assert result["cleared"] is False
    assert loads == []
    assert path.read_bytes() == before


@pytest.mark.parametrize("exit_path", ["reload_raises", "reload_none", "already_cleared", "written_since_reload", "cleared"])
def test_lock_is_released_on_every_exit(store, monkeypatch, exit_path):
    import api.config as config
    import api.models as models
    import api.routes as routes

    path = _write(store, "webui", partials=False)
    stub = models.Session.load_metadata_only(SID)
    # Hold a reference: the lock registry is weak, so an unreferenced leaked lock
    # would be collected and a fresh lookup would hide the leak.
    lock = config._get_session_agent_lock(SID)
    if exit_path == "reload_raises":
        monkeypatch.setattr(models, "get_session", lambda *a, **k: (_ for _ in ()).throw(OSError("gone")))
    elif exit_path == "reload_none":
        monkeypatch.setattr(models, "get_session", lambda *a, **k: None)
    elif exit_path == "already_cleared":
        full = models.Session.load(SID)
        full.active_stream_id = None
        monkeypatch.setattr(models, "get_session", lambda *a, **k: full)
    elif exit_path == "written_since_reload":
        monkeypatch.setattr(models, "_sidecar_unchanged_since", lambda *a, **k: False)
    before = path.read_bytes()
    cleared = routes._clear_stale_stream_state(stub)
    assert cleared is (exit_path == "cleared")
    assert not lock.locked()
    assert lock is config._get_session_agent_lock(SID)
    if exit_path == "written_since_reload":
        assert path.read_bytes() == before
        assert stub.active_stream_id == "dead-stream"
    if exit_path in ("already_cleared", "cleared"):
        assert stub.active_stream_id is None


def _write_partials_only(store):
    """A modern sidecar with duplicate partials and no stream, for the load's repair save."""
    path = _write(store, "webui", partials=True)
    body = json.loads(path.read_text(encoding="utf-8"))
    body.pop("active_stream_id")
    path.write_text(json.dumps(body), encoding="utf-8")
    return path


def test_load_repair_save_persists_when_nobody_else_wrote(store):
    import api.models as models

    directory, _home, _sessions = store
    path = _write_partials_only(store)
    session = models.Session.load(SID)
    assert len(session.messages) == 2
    assert len(json.loads(path.read_text(encoding="utf-8"))["messages"]) == 2
    assert list(directory.glob(f"{SID}.json.bak"))


def test_load_under_a_held_lock_keeps_the_collapse_in_memory(store):
    """A caller that already holds the session lock (it isn't reentrant) gets the collapsed
    transcript without a save or a deadlock; its own save persists it."""
    import api.config as config
    import api.models as models

    path = _write_partials_only(store)
    before = path.read_bytes()
    with config._get_session_agent_lock(SID):
        session = models.Session.load(SID)
        assert len(session.messages) == 2
        assert path.read_bytes() == before
        session.save(touch_updated_at=False)
    assert len(json.loads(path.read_text(encoding="utf-8"))["messages"]) == 2


@pytest.mark.parametrize("writer", ["in_process", "outside"])
def test_load_skips_the_repair_save_when_the_file_changed_after_the_read(store, monkeypatch, writer):
    """Another writer replaces the file after the load read it: the load keeps its collapse in
    memory and the newer file stays."""
    import api.models as models

    path = _write_partials_only(store)
    real_collapse = models._collapse_adjacent_duplicate_partials
    newer = json.loads(path.read_text(encoding="utf-8"))
    newer["messages"] = newer["messages"] + [{"role": "user", "content": "newer turn", "timestamp": NOW}]
    newer["message_count"] = len(newer["messages"])
    fired = []

    def _write_newer():
        if writer == "in_process":
            other = models.Session(**{k: v for k, v in newer.items() if k not in ("message_count", "anchor_scene_index")})
            other.save(touch_updated_at=False)
        else:
            path.write_text(json.dumps(newer), encoding="utf-8")

    def _collapse_then_write(messages):
        result = real_collapse(messages)
        if not fired:
            fired.append(True)
            worker = threading.Thread(target=_write_newer)
            worker.start()
            worker.join(10)
        return result

    monkeypatch.setattr(models, "_collapse_adjacent_duplicate_partials", _collapse_then_write)
    session = models.Session.load(SID)
    assert fired and len(session.messages) == 2
    on_disk = json.loads(path.read_text(encoding="utf-8"))
    assert any(m.get("content") == "newer turn" for m in on_disk["messages"])
