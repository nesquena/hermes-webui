"""#9007: listing sessions never saves one.

``Session.load(sid)`` repairs as it reads: a sidecar with adjacent duplicate partials is collapsed and
saved back from the freshly loaded, detached object. Three read-only paths behind ``GET /api/sessions``
called it: the #4985 orphan-prune probe, the ``all_sessions()`` index backfill and its full-scan
fallback. They run for sessions the requesting profile doesn't own and without the session's lock, so a
poll from ``default`` could save ``work``'s session from an old copy while the owner checkpointed a newer
turn, and the newer turn was lost. The three paths now read through ``_load_session_for_listing``, which
collapses in memory only; what the listing shows is unchanged.
"""

import io
import json
import sqlite3
import threading
import time
from collections import OrderedDict
from urllib.parse import urlparse

import pytest

SID = "listing-9007"
ANCHOR = "anchor-9007"
PARTIAL = {"role": "assistant", "content": "same", "_partial": True}
POLL_THREAD = "foreign-sidebar-poll-9007"


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
    # Each profile's agent state.db lives under the test home; nothing falls back to a real one.
    monkeypatch.setattr(models, "_get_profile_home", lambda profile: home / "profiles" / str(profile or "default"))
    monkeypatch.setattr(models, "_active_state_db_path", lambda: home / "state.db")
    # A missing index must reach the full scan, not race a background rebuild.
    monkeypatch.setattr(models, "_start_session_index_rebuild_thread", lambda: None)
    routes._session_list_cache_clear()
    yield directory, home
    routes._session_list_cache_clear()


def _sidecar(directory, home, *, partials=True):
    """`work`'s session; with ``partials`` its transcript ends in two identical partials."""
    messages = [{"role": "user", "content": "hello", "timestamp": 1.5}]
    if partials:
        messages += [dict(PARTIAL), dict(PARTIAL)]
    else:
        messages += [{"role": "assistant", "content": "hi", "timestamp": 1.6}]
    body = {
        "session_id": SID, "title": "Private", "created_at": 1.0, "updated_at": 2.0,
        "workspace": str(home / "workspace"), "source_tag": "webui",
        "message_count": len(messages), "anchor_scene_index": {}, "profile": "work",
        "messages": messages,
    }
    path = directory / f"{SID}.json"
    path.write_text(json.dumps(body), encoding="utf-8")
    return path


def _anchor(directory):
    """`default`'s own session, so the index is never empty."""
    row = {"session_id": ANCHOR, "title": "Anchor", "profile": "default", "created_at": 3.0,
           "updated_at": 3.0, "message_count": 1}
    body = dict(row, anchor_scene_index={}, messages=[{"role": "user", "content": "a", "timestamp": 3.0}])
    (directory / f"{ANCHOR}.json").write_text(json.dumps(body), encoding="utf-8")
    return dict(row, last_message_at=3.0)


def _index(directory, rows):
    (directory / "_index.json").write_text(json.dumps(rows), encoding="utf-8")


def _empty_state_db(home, profile="work"):
    """`profile`'s agent state.db: a row for SID and no messages (the #4985 prune trigger)."""
    path = home / "profiles" / profile / "state.db"
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path)
    try:
        conn.execute(
            "CREATE TABLE sessions (id TEXT PRIMARY KEY, source TEXT, title TEXT, started_at REAL, "
            "ended_at REAL, message_count INTEGER, model TEXT, cwd TEXT, parent_session_id TEXT)"
        )
        conn.execute(
            "CREATE TABLE messages (id INTEGER PRIMARY KEY, session_id TEXT, role TEXT, content TEXT, "
            "timestamp REAL)"
        )
        conn.execute("INSERT INTO sessions VALUES (?,?,?,?,?,?,?,?,?)",
                     (SID, "webui", "Private", 1.0, None, 0, "test", str(home / "workspace"), None))
        conn.commit()
    finally:
        conn.close()


def _setup(store, site, *, partials=True):
    """Lay out the store so ``site``'s read is the one that loads SID's sidecar."""
    directory, home = store
    path = _sidecar(directory, home, partials=partials)
    anchor = _anchor(directory)
    if site == "prune":
        _index(directory, [anchor])  # SID's row is missing from the index
        _empty_state_db(home)
    elif site == "backfill":
        _index(directory, [anchor, {"session_id": SID, "title": "Private", "profile": "work",
                                    "created_at": 1.0, "updated_at": 2.0, "message_count": 2}])
    else:
        assert site == "full-scan"  # no index at all
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


def _list(profile):
    from api import profiles, routes

    routes._session_list_cache_clear()
    handler = _Handler()
    profiles.set_request_profile(profile)
    try:
        routes.handle_get(handler, urlparse("/api/sessions"))
    finally:
        profiles.clear_request_profile()
    assert handler.status == 200, handler.status
    return json.loads(handler.wfile.getvalue().decode("utf-8"))["sessions"]


@pytest.fixture
def saves(monkeypatch):
    """Every Session.save call, by session id; the saves still happen."""
    from api import models

    calls = []
    real_save = models.Session.save

    def spy(self, *args, **kwargs):
        calls.append(self.session_id)
        return real_save(self, *args, **kwargs)

    monkeypatch.setattr(models.Session, "save", spy)
    return calls


@pytest.fixture
def reads(monkeypatch):
    """The functions on the stack each time SID's partials are collapsed, so a test can show
    the listing really read the sidecar through the path it targets."""
    import traceback

    from api import models

    stacks = []
    real = models._collapse_adjacent_duplicate_partials

    def spy(messages):
        if isinstance(messages, list) and sum(1 for m in messages if m == PARTIAL) >= 2:
            stacks.append({frame.name for frame in traceback.extract_stack()})
        return real(messages)

    monkeypatch.setattr(models, "_collapse_adjacent_duplicate_partials", spy)
    return stacks


@pytest.mark.parametrize("site,reader", [
    ("prune", "_prune_orphaned_webui_zero_message_sessions"),
    ("backfill", "all_sessions"),
    ("full-scan", "all_sessions"),
])
def test_another_profiles_listing_never_saves_a_session_it_reads(store, saves, reads, site, reader):
    directory, _ = store
    path = _setup(store, site)
    before = (path.read_bytes(), path.stat().st_mtime_ns)

    rows = _list("default")

    assert any(reader in stack for stack in reads), (site, reads)
    assert SID not in saves, f"listing from `default` saved `work`'s session ({site})"
    assert (path.read_bytes(), path.stat().st_mtime_ns) == before
    assert not list(directory.rglob("*.bak*"))
    assert SID not in {row.get("session_id") for row in rows}
    if site == "backfill":
        # The backfill itself still lands, in the index only.
        index = {row["session_id"]: row for row in json.loads((directory / "_index.json").read_text())}
        assert index[SID]["last_message_at"] == 1.5


# (title, message_count, last_message_at, profile) as exp-v0.52.672 listed them, before this change.
# The prune row comes from missing-index recovery's metadata stub, so it shows the sidecar's own
# count and updated_at; the duplicate-partial rows show the sidecar's persisted count (3).
MASTER_ROWS = {
    ("prune", False): ("Private", 2, 2.0, "work"),
    ("prune", True): ("Private", 3, 2.0, "work"),
    ("backfill", False): ("Private", 2, 1.6, "work"),
    ("backfill", True): ("Private", 3, 1.5, "work"),
    ("full-scan", False): ("Private", 2, 1.6, "work"),
    ("full-scan", True): ("Private", 3, 1.5, "work"),
}


@pytest.mark.parametrize("site", ["prune", "backfill", "full-scan"])
@pytest.mark.parametrize("partials", [False, True], ids=["ordinary", "duplicate-partials"])
def test_the_owners_listing_row_is_unchanged(store, site, partials):
    _setup(store, site, partials=partials)

    rows = {row["session_id"]: row for row in _list("work")}

    row = rows[SID]
    shown = (row.get("title"), row.get("message_count"), row.get("last_message_at"), row.get("profile"))
    assert shown == MASTER_ROWS[(site, partials)], shown


def test_a_foreign_poll_never_overwrites_a_turn_the_owner_checkpoints(store, monkeypatch):
    """Adapted from the #9007 repro: `default`'s poll reads `work`'s session, the owner checkpoints a
    newer accepted turn under the session lock, then the poll finishes. The turn must survive."""
    import api.config as config
    from api import models

    directory, _ = store
    path = _setup(store, "prune")
    read_done, resume, poll_done = threading.Event(), threading.Event(), threading.Event()
    errors = []
    real = models._collapse_adjacent_duplicate_partials

    def pausing_collapse(messages):
        result = real(messages)
        if threading.current_thread().name == POLL_THREAD and result[1] and not read_done.is_set():
            # The poll holds its (now old) copy of the session; let the owner write a newer turn.
            read_done.set()
            if not resume.wait(15):
                raise RuntimeError("barrier timed out")
        return result

    monkeypatch.setattr(models, "_collapse_adjacent_duplicate_partials", pausing_collapse)

    def poll():
        try:
            _list("default")
        except Exception as exc:  # surfaced below
            errors.append(repr(exc))
        finally:
            poll_done.set()

    thread = threading.Thread(target=poll, name=POLL_THREAD)
    thread.start()
    try:
        deadline = time.monotonic() + 15
        while not read_done.is_set() and not poll_done.is_set() and time.monotonic() < deadline:
            time.sleep(0.005)
        assert read_done.is_set(), "the poll never read the sidecar"
        with config._get_session_agent_lock(SID):
            current = models.Session.load(SID)
            current.active_stream_id = "new-live-stream"
            current.pending_user_message = "accepted next prompt"
            current.pending_started_at = time.time()
            current.messages.append({"role": "user", "content": "accepted next prompt",
                                     "timestamp": current.pending_started_at})
            with config.LOCK:
                models.SESSIONS[SID] = current
            monkeypatch.setitem(config.STREAMS, "new-live-stream", config.create_stream_channel())
            current.save(touch_updated_at=False)
    finally:
        resume.set()
        thread.join(15)
    assert not errors, errors

    after = json.loads(path.read_text(encoding="utf-8"))
    assert len(after["messages"]) == 3
    assert after["pending_user_message"] == "accepted next prompt"
    assert after["active_stream_id"] == "new-live-stream"

    models.SESSIONS.clear()
    cold = models.get_session(SID)
    assert len(cold.messages) == 3
    assert cold.pending_user_message == "accepted next prompt"
    assert cold.active_stream_id == "new-live-stream"


OTHER = "other-9007"


def _kind(directory, home, kind):
    """Lay out one sidecar shape; return the id the listing reads it by."""
    from api import models

    if kind in ("unsafe", "missing"):
        if kind == "missing":
            return SID
        # A real sidecar sits where the traversal id points.
        _sidecar(directory, home)
        (directory / f"{SID}.json").rename(directory.parent / "escape-9007.json")
        return "../escape-9007"
    partials = kind in ("duplicate-partials", "names-other", "legacy-duplicate-partials")
    path = _sidecar(directory, home, partials=partials)
    if kind in ("corrupt", "empty"):
        path.write_text("{not json" if kind == "corrupt" else "", encoding="utf-8")
        return SID
    body = json.loads(path.read_text(encoding="utf-8"))
    if kind in ("legacy", "legacy-duplicate-partials"):
        body.pop("anchor_scene_index")
        body.pop("message_count")
    elif kind == "names-other":
        body["session_id"] = OTHER
    elif kind == "no-session-id":
        body.pop("session_id")
    elif kind == "share-fields-after-body":
        body.update(share_token="tok-9007", share_created_at=5.0)
    elif kind == "checkpoint-fact":
        # An eager-checkpoint fact the writer vouched for, whose row this transcript lacks.
        body.update(pending_user_source="delegation_wakeup", pending_user_message="wake",
                    pending_started_at=7.0, _pending_hidden_wakeup_at=7.0)
    path.write_text(json.dumps(body), encoding="utf-8")
    # A draft under each id, so a draft read under the wrong id shows.
    for draft_id in (SID, OTHER):
        draft = models._session_draft_path(draft_id)
        draft.parent.mkdir(parents=True, exist_ok=True)
        draft.write_text(json.dumps({"text": f"draft for {draft_id}"}), encoding="utf-8")
    return SID


def _outcome(read, sid):
    from api import models

    models._LEGACY_SIDECAR_FACTS.clear()
    try:
        session = read(sid)
    except Exception as exc:
        return ("raises", type(exc).__name__)
    if session is None:
        return None
    compact = session.compact(include_runtime=True, active_stream_ids=set())
    if compact["session_id"] not in (SID, OTHER):
        compact["session_id"] = "<made up: the body names no session>"
    return {
        "compact": compact,
        "messages": session.messages,
        "composer_draft": session.composer_draft,
        "names_other_session": getattr(session, "_sidecar_names_other_session", None),
        "share_fields_misplaced": getattr(session, "_share_fields_misplaced", None),
        "legacy_facts": [dict(entry) for entry in models._LEGACY_SIDECAR_FACTS.values()],
    }


@pytest.mark.parametrize("kind", [
    "ordinary", "duplicate-partials", "legacy", "legacy-duplicate-partials", "names-other",
    "no-session-id", "share-fields-after-body", "checkpoint-fact", "corrupt", "empty", "missing",
    "unsafe",
])
def test_the_listing_read_is_session_load_without_the_save(store, saves, kind):
    """What the three listing paths get back is what Session.load returned them, on every sidecar
    shape they can meet; only the save is gone."""
    from api import models

    directory, home = store
    sid = _kind(directory, home, kind)
    path = directory / f"{SID}.json"
    before = path.read_bytes() if path.exists() else None

    listed = _outcome(models._load_session_for_listing, sid)
    assert saves == []
    assert (path.read_bytes() if path.exists() else None) == before
    loaded = _outcome(models.Session.load, sid)  # may repair-save; it runs second

    assert listed == loaded
