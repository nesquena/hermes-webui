"""#8852: a sidecar whose body names another session never writes that session.

``Session.load(sid)`` builds the session from the file's body, so ``session.session_id`` is
whatever the body says, and ``save()`` writes ``SESSION_DIR / f"{session_id}.json"``. A sidecar
whose body names a different session (a manual copy, or a backup restored under another name)
therefore overwrote that other session:

- on its first load, through the duplicate-partial collapse save (no request needed: an index
  rebuild loads every file);
- on any later save of the loaded object, whatever triggered it.

A sidecar with no ``session_id`` at all got a random id, and each collapse load wrote a new
stray file under it.

``Session.load`` (and the metadata-only loader) now marks a session whose body doesn't name the
file it was read from, keeps the collapse in memory only, and ``save()`` refuses to write a marked
session; so does the one writer that patches a sidecar without ``save()`` (workspace recovery).
``POST /api/chat/start`` answers a marked session 404 before mutating anything. What a load returns and
what the session list shows are unchanged. The server never writes such a file itself: every
sidecar it saves carries its own id.
"""

import io
import json
import logging
import urllib.parse
from collections import OrderedDict
from pathlib import Path

import pytest

VICTIM = "victim-8852"
COPY = "copy-8852"


@pytest.fixture
def store(tmp_path, monkeypatch):
    import api.config as config
    import api.models as models
    import api.profiles as profiles
    import api.routes as routes

    sessions = OrderedDict()
    directory = tmp_path / "sessions"
    directory.mkdir()
    monkeypatch.setattr(models, "SESSION_DIR", directory)
    monkeypatch.setattr(models, "SESSIONS", sessions)
    monkeypatch.setattr(routes, "SESSIONS", sessions)
    index = directory / "_index.json"
    for module in (config, models, routes):
        if hasattr(module, "SESSION_INDEX_FILE"):
            monkeypatch.setattr(module, "SESSION_INDEX_FILE", index)
    routes._session_list_cache_clear()
    monkeypatch.setattr(routes, "_get_active_profile_name", lambda: "default")
    monkeypatch.setattr(profiles, "get_active_profile_name", lambda: "default")
    yield directory
    routes._session_list_cache_clear()


def _victim(directory, workspace=None):
    """A real 5-turn session owned by `work`."""
    from api import models

    session = models.Session(session_id=VICTIM, title="Victim", profile="work")
    if workspace is not None:
        session.workspace = str(workspace)
    for i in range(5):
        session.messages += [
            {"role": "user", "content": f"q{i}", "timestamp": 100 + 2 * i},
            {"role": "assistant", "content": f"a{i}", "timestamp": 101 + 2 * i},
        ]
    session.save(touch_updated_at=False)
    models.SESSIONS.clear()
    return directory / f"{VICTIM}.json"


def _copy(directory, *, partials=True, body_id=VICTIM, name=COPY):
    """A sidecar named `name` whose body names `body_id` (None: no session_id key at all)."""
    raw = json.loads((directory / f"{VICTIM}.json").read_text(encoding="utf-8"))
    raw.update(title="Copy", profile="default")
    if body_id is None:
        raw.pop("session_id")
    else:
        raw["session_id"] = body_id
    messages = [{"role": "user", "content": "copy q", "timestamp": 500}]
    if partials:
        partial = {"role": "assistant", "content": "partial", "timestamp": 501, "_partial": True}
        messages += [dict(partial), dict(partial)]
    else:
        messages.append({"role": "assistant", "content": "copy a", "timestamp": 501})
    raw["messages"] = messages
    raw["message_count"] = len(messages)
    path = directory / f"{name}.json"
    path.write_text(json.dumps(raw, indent=2), encoding="utf-8")
    return path


def _files(directory):
    """Every session file's bytes; the index is excluded (a rebuild legitimately writes it)."""
    return {
        p.name: p.read_bytes()
        for p in sorted(directory.iterdir())
        if p.is_file() and not p.name.startswith("_index")
    }


def _unchanged_victim(directory, before):
    after = _files(directory)
    assert after.get(f"{VICTIM}.json") == before[f"{VICTIM}.json"], "the victim's conversation was overwritten"
    assert f"{VICTIM}.json.bak" not in after, "a shrink backup of the victim was written"
    victim = json.loads(after[f"{VICTIM}.json"])
    assert (len(victim["messages"]), victim["profile"], victim["title"]) == (10, "work", "Victim")


# --- the copy never writes the session its body names -----------------------------------


def test_loading_a_copy_never_writes_the_session_its_body_names(store):
    from api import models

    _victim(store)
    _copy(store)
    before = _files(store)

    session = models.Session.load(COPY)

    _unchanged_victim(store, before)
    assert _files(store) == before, "loading wrote a file"
    # What a load returns is unchanged: the body's id, with the partials collapsed in memory.
    assert session.session_id == VICTIM
    assert [m["content"] for m in session.messages] == ["copy q", "partial"]


def test_repeated_loads_of_a_copy_never_write(store):
    from api import models

    _victim(store)
    _copy(store)
    before = _files(store)
    for _ in range(3):
        models.SESSIONS.clear()
        models.Session.load(COPY)
    assert _files(store) == before


def test_an_index_rebuild_never_writes_the_session_a_copy_names(store):
    """No request needed on master: listing sessions with the index missing rebuilds it,
    and the rebuild loads every sidecar, the copy included."""
    from api import models

    _victim(store)
    _copy(store)
    models.SESSION_INDEX_FILE.unlink()  # the victim's own save wrote it
    models.SESSIONS.clear()
    before = _files(store)

    models.all_sessions()

    _unchanged_victim(store, before)
    assert _files(store) == before


@pytest.mark.parametrize("partials", [True, False])
def test_a_later_save_of_a_loaded_copy_is_refused(store, partials):
    """Whatever later saves the loaded copy (a mutation, a repair) would write the session its
    body names, partials or not. The save is refused and nothing is written."""
    from api import models

    _victim(store)
    _copy(store, partials=partials)
    before = _files(store)

    session = models.get_session(COPY)
    session.title = "Mutated"
    with pytest.raises(RuntimeError, match="8852"):
        session.save()

    _unchanged_victim(store, before)
    assert _files(store) == before


@pytest.mark.parametrize("messages", [1, 0])
def test_opening_a_copy_over_http_never_writes_the_session_it_names(store, messages):
    """``GET /api/session`` on the copy gives the same answer as before (it isn't refused by
    this fix), and writes nothing."""
    from api.routes import handle_get

    _victim(store)
    _copy(store)
    before = _files(store)

    handler = _Handler()
    handle_get(handler, urllib.parse.urlparse(f"/api/session?session_id={COPY}&messages={messages}"))

    assert handler.status in (200, 404, 409), handler.status
    _unchanged_victim(store, before)
    assert _files(store) == before


@pytest.mark.parametrize("partials", [True, False])
def test_sending_to_a_copy_never_takes_over_the_session_it_names(store, monkeypatch, partials):
    """On master, ``POST /api/chat/start`` with the copy's id started a turn (200), overwrote the
    victim with the copy's transcript, profile and title, and the victim's owner then got 409
    "Session belongs to a different profile" on their own conversation. Now it answers 404 (the
    generic ownership guard's answer) before anything is mutated: no turn starts, nothing is
    written, the cached copy isn't left mid-turn, and the owner carries on. (#8853 additionally
    refuses this request before the load, because the copy's ownership can't be verified.)"""
    import api.profiles as profiles
    import api.routes as routes
    from api import models

    ran = []
    monkeypatch.setattr(routes, "_run_agent_streaming", lambda *a, **k: ran.append("agent"))
    _victim(store)
    _copy(store, partials=partials)
    before = _files(store)

    handler = _Handler()
    routes._handle_chat_start(handler, {"session_id": COPY, "message": "hi", "profile": "default"})
    assert handler.status == 404, bytes(handler.body)
    assert ran == []
    _unchanged_victim(store, before)
    assert _files(store) == before
    for cached in (models.SESSIONS.get(COPY), models.SESSIONS.get(VICTIM)):
        if cached is not None:
            assert not cached.active_stream_id and not cached.pending_user_message

    monkeypatch.setattr(routes, "_get_active_profile_name", lambda: "work")
    monkeypatch.setattr(profiles, "get_active_profile_name", lambda: "work")
    owner = _Handler()
    routes._handle_chat_start(owner, {"session_id": VICTIM, "message": "owner turn", "profile": "work"})
    assert owner.status == 200, bytes(owner.body)
    assert ran == ["agent"]
    victim = json.loads((store / f"{VICTIM}.json").read_text(encoding="utf-8"))
    assert (len(victim["messages"]), victim["profile"], victim["pending_user_message"]) == (10, "work", "owner turn")


# --- writers that don't go through save() -----------------------------------------------


@pytest.fixture
def deleted_workspace(store, tmp_path, monkeypatch):
    """The victim's (and so the copy's) workspace folder has been deleted; workspace recovery
    rebinds it to a fallback folder by patching the sidecar directly, without ``save()``."""
    import api.routes as routes
    import api.workspace as workspace

    gone = tmp_path / "deleted-workspace"
    fallback = tmp_path / "fallback-workspace"
    fallback.mkdir()

    def resolver(candidate, fallback_getter, profile=None):
        if str(candidate or "") == str(gone):
            return fallback.resolve(), True
        return Path(candidate or fallback).resolve(), False

    monkeypatch.setattr(routes, "resolve_implicit_workspace_with_recovery", resolver)
    monkeypatch.setattr(workspace, "resolve_implicit_workspace_with_recovery", resolver)
    return gone


def test_sending_to_a_copy_whose_workspace_was_deleted_writes_nothing(store, monkeypatch, deleted_workspace):
    """The workspace-recovery writer took its target from the copy's (body) id, so before the save
    guard was ever reached it rebound the victim to the requester's fallback folder and rewrote the
    victim's index row from the copy (senior review, round 1)."""
    import api.routes as routes

    monkeypatch.setattr(routes, "_run_agent_streaming", lambda *a, **k: None)
    _victim(store, workspace=deleted_workspace)
    _copy(store, partials=False)
    before = _files(store)

    handler = _Handler()
    routes._handle_chat_start(handler, {"session_id": COPY, "message": "hi", "profile": "default"})

    assert handler.status == 404, bytes(handler.body)
    _unchanged_victim(store, before)
    assert json.loads(_files(store)[f"{VICTIM}.json"])["workspace"] == str(deleted_workspace)
    assert _files(store) == before


@pytest.mark.parametrize("layout", ["modern", "legacy"])
def test_file_operations_on_a_copy_never_write_the_session_it_names(store, monkeypatch, deleted_workspace, layout):
    """``get_session_for_file_ops`` loads metadata only and runs the same workspace recovery. Over
    HTTP the generic ownership guard 404s the copy first; this covers a direct caller."""
    from api import models

    _victim(store, workspace=deleted_workspace)
    path = _copy(store, partials=False)
    if layout == "legacy":
        # A pre-#5854 layout: no message_count in the prefix, so the stub comes from the
        # legacy-facts cache branch.
        raw = json.loads(path.read_text(encoding="utf-8"))
        raw.pop("message_count", None)
        raw.pop("anchor_scene_index", None)
        path.write_text(json.dumps(raw, indent=2), encoding="utf-8")
        monkeypatch.setattr(models, "_legacy_sidecar_facts_get", lambda sid: {"message_count": 2, "scene_index": {}})
    before = _files(store)

    stub = models.Session.load_metadata_only(COPY)
    assert getattr(stub, "_loaded_metadata_only", False) and stub.session_id == VICTIM
    assert getattr(stub, "_sidecar_names_other_session", None) == COPY
    models.SESSIONS.clear()
    with pytest.raises(models.WorkspaceBindingPersistenceError):
        models.get_session_for_file_ops(COPY)

    _unchanged_victim(store, before)
    assert _files(store) == before


def test_the_workspace_writer_refuses_a_session_loaded_from_a_copy(store, tmp_path):
    from api import models

    _victim(store)
    _copy(store, partials=False)
    before = _files(store)
    session = models.Session.load(COPY)
    target = tmp_path / "elsewhere"
    target.mkdir()

    with pytest.raises(models.WorkspaceBindingPersistenceError, match="different session"):
        models.persist_recovered_workspace_binding(session, target, expected_workspace=session.workspace)

    _unchanged_victim(store, before)
    assert _files(store) == before


def _mid_turn_copy(store, *, gateway=False):
    """A copy of a session caught mid-turn: empty transcript, a pending message and a stream id
    that no longer exists (optionally a Gateway run that would be reattached at startup)."""
    _victim(store)
    path = _copy(store, partials=False)
    raw = json.loads(path.read_text(encoding="utf-8"))
    raw.update(messages=[], message_count=0, pending_user_message="half-sent", active_stream_id="dead-stream")
    raw.pop("pending_started_at", None)
    if gateway:
        raw["gateway_run"] = {"run_id": "run-8852", "stream_id": "dead-stream", "regeneration": False, "goal_related": False}
    # Keep the header layout the server writes: active_stream_id must sit in the prefix the
    # startup scan reads.
    path.write_text(json.dumps(raw, indent=2), encoding="utf-8")
    return path


@pytest.mark.parametrize("entry", ["get_session", "clear_stale_stream_state", "detail_get"])
def test_a_copy_taken_mid_turn_loads_quietly(store, caplog, entry):
    """A copy of a session caught mid-turn runs a stale-state repair on load: the cold-load
    repair (``_repair_stale_pending``) and the detail GET's ``_clear_stale_stream_state``. On master
    both saved the copy over the other session; with the save refused, each logged an ERROR
    traceback naming the other session. Now neither persists anything for a marked session.
    ``_clear_stale_stream_state`` still clears the dead stream from the in-memory view, so the
    client doesn't keep reconnecting to it. (With #8853 the detail GET refuses a copy with 404
    before the load; the direct case covers the clear either way.)"""
    import api.routes as routes
    from api import models

    _mid_turn_copy(store)
    before = _files(store)

    with caplog.at_level(logging.WARNING):
        for _ in range(2):
            models.SESSIONS.clear()
            if entry == "get_session":
                session = models.get_session(COPY)
                state = (session.pending_user_message, session.active_stream_id)
                expected = ("half-sent", "dead-stream")
            elif entry == "clear_stale_stream_state":
                session = models.get_session(COPY)
                assert routes._clear_stale_stream_state(session) is False
                state = (session.pending_user_message, session.active_stream_id)
                expected = (None, None)
            else:
                handler = _Handler()
                routes.handle_get(handler, urllib.parse.urlparse(f"/api/session?session_id={COPY}&messages=1"))
                assert handler.status in (200, 404), bytes(handler.body)
                if handler.status == 200:
                    body = json.loads(bytes(handler.body))["session"]
                    state = (body.get("pending_user_message"), body.get("active_stream_id"))
                else:
                    state = None
                expected = (None, None) if handler.status == 200 else None

    assert _files(store) == before
    assert [r.getMessage() for r in caplog.records if r.levelno >= logging.ERROR] == []
    assert state == expected


def test_a_copy_of_a_turn_that_is_still_running_keeps_showing_it(store):
    """If the stream the copy names is live (the other session's turn is running), the view is
    left alone, as on master: only a dead stream is cleared from the view."""
    import api.config as config
    import api.routes as routes
    from api import models

    _mid_turn_copy(store)
    before = _files(store)
    with config.STREAMS_LOCK:
        config.STREAMS["dead-stream"] = object()
    try:
        session = models.get_session(COPY)
        assert routes._clear_stale_stream_state(session) is False
        state = (session.pending_user_message, session.active_stream_id)
    finally:
        with config.STREAMS_LOCK:
            config.STREAMS.pop("dead-stream", None)

    assert state == ("half-sent", "dead-stream")
    assert _files(store) == before


def test_the_reasoning_writer_refuses_a_session_loaded_from_a_copy(store):
    """``set_session_reasoning_effort`` given a session object patches that object's sidecar
    directly, without ``save()``. Over HTTP the generic ownership guard 404s the copy first
    (``/api/reasoning``); this covers a direct caller."""
    from api import models

    _victim(store)
    _copy(store, partials=False)
    before = _files(store)

    with pytest.raises(RuntimeError, match="8852"):
        models.set_session_reasoning_effort(models.get_session(COPY), "high")

    _unchanged_victim(store, before)
    assert _files(store) == before


def test_the_title_writer_refuses_a_session_loaded_from_a_copy(store):
    """``_persist_generated_session_title`` reloads the session the object's id names and saves
    it, so a copy object would retitle the session its body names. Over HTTP the generic
    ownership guard 404s the copy first (``/api/session/title/regenerate``), and the import
    title thread loads the imported session by its own id; this covers a direct caller."""
    from api import models, routes

    _victim(store)
    _copy(store, partials=False)
    before = _files(store)

    with pytest.raises(RuntimeError, match="8852"):
        routes._persist_generated_session_title(
            models.get_session(COPY), "Hijacked by copy", event_reason="test"
        )

    _unchanged_victim(store, before)
    assert _files(store) == before
    assert json.loads(before[f"{VICTIM}.json"])["title"] == "Victim"


def test_gateway_reattach_at_startup_skips_a_copy(store, monkeypatch):
    """At startup every sidecar with an active stream and a saved Gateway run is reattached under
    its session's id. A copy's id is the other session's, so its stale run would be resumed, and
    its answer written back, there."""
    import api.config as config
    import api.gateway_chat as gateway_chat

    launched = []
    monkeypatch.setattr(gateway_chat, "_run_gateway_chat_streaming", lambda *a, **k: launched.append((a, k)))
    monkeypatch.setattr(gateway_chat, "_gateway_endpoint_for_profile", lambda profile: "http://127.0.0.1:9")
    _mid_turn_copy(store, gateway=True)
    before = _files(store)
    try:
        resumed = gateway_chat.resume_gateway_runs_after_restart()
        with config.STREAMS_LOCK:
            registered = "dead-stream" in config.STREAMS
    finally:
        with config.STREAMS_LOCK:
            config.STREAMS.pop("dead-stream", None)
        config.CANCEL_FLAGS.pop("dead-stream", None)

    assert (resumed, registered, launched) == ([], False, [])
    _unchanged_victim(store, before)
    assert _files(store) == before


@pytest.mark.parametrize("partials", [True, False])
def test_a_background_wakeup_for_a_copy_is_dropped_cleanly(store, monkeypatch, caplog, partials):
    """``start_session_turn`` (process and delegation wakeups) is the server-side twin of chat/start.
    On a copy it raised from the save guard, which its contract says never happens, and left the
    cached copy mid-turn; the process-wakeup runner re-queued the wakeup and logged a traceback on
    every drain (senior review, round 3). It now answers the 404 its callers drop, before anything
    is mutated, and the owner's own wakeup still starts."""
    import api.routes as routes
    from api import models

    ran = []
    monkeypatch.setattr(routes, "_run_agent_streaming", lambda *a, **k: ran.append("agent"))
    _victim(store)
    _copy(store, partials=partials)
    before = _files(store)

    with caplog.at_level(logging.WARNING):
        resp = routes.start_session_turn(COPY, "wake up")

    assert (resp.get("_status"), resp.get("error")) == (404, "Session not found")
    assert ran == []
    _unchanged_victim(store, before)
    assert _files(store) == before
    cached = models.SESSIONS.get(COPY)
    if cached is not None:
        assert not cached.active_stream_id and not cached.pending_user_message
    assert [r.getMessage() for r in caplog.records if r.levelno >= logging.WARNING] == []

    owner = routes.start_session_turn(VICTIM, "owner wake")
    assert owner.get("_status", 200) == 200, owner
    assert ran == ["agent"]


def test_a_sidecar_without_a_session_id_never_spawns_files(store):
    """``Session(**data)`` gives a body without ``session_id`` a random id; each collapse load,
    index rebuilds included, used to write a new stray file under it."""
    from api import models

    _victim(store)
    _copy(store, body_id=None)
    before = _files(store)

    models.Session.load(COPY)
    for _ in range(2):
        models.SESSIONS.clear()
        if models.SESSION_INDEX_FILE.exists():
            models.SESSION_INDEX_FILE.unlink()
        models.all_sessions()

    assert _files(store) == before


# --- what still writes, as before ----------------------------------------------------------


def test_a_session_whose_body_names_its_own_file_still_persists_its_collapse(store):
    """The #2592 self-heal is unchanged for every sidecar the server writes."""
    from api import models

    _victim(store)
    path = _copy(store, body_id=COPY)

    session = models.Session.load(COPY)

    data = json.loads(path.read_text(encoding="utf-8"))
    assert [m["content"] for m in data["messages"]] == ["copy q", "partial"]
    assert path.with_suffix(".json.bak").exists()
    assert session.session_id == COPY
    assert not getattr(session, "_sidecar_names_other_session", None)


def test_a_normal_session_still_saves(store):
    from api import models

    path = _victim(store)
    session = models.get_session(VICTIM)
    session.title = "Renamed"
    session.save()
    assert json.loads(path.read_text(encoding="utf-8"))["title"] == "Renamed"


def test_a_loaded_session_can_still_be_saved_under_a_new_id(store):
    """Compression rotation (``api/streaming.py``) loads a session, sets a new ``session_id`` and
    saves it there. Only a session whose file body names another session is marked."""
    from api import models

    _victim(store)
    session = models.Session.load(VICTIM)
    session.session_id = "rotated-8852"
    session.save()
    assert (store / "rotated-8852.json").exists()
    assert json.loads((store / "rotated-8852.json").read_text(encoding="utf-8"))["session_id"] == "rotated-8852"


class _Handler:
    def __init__(self):
        self.status = None
        self.sent_headers = []
        self.body = bytearray()
        self.wfile = self
        self.rfile = io.BytesIO(b"")
        self.headers = {"Content-Length": "0"}
        self.request = None
        self.client_address = ("127.0.0.1", 12345)
        self.command = "GET"

    def send_response(self, status):
        self.status = status

    def send_header(self, name, value):
        self.sent_headers.append((name, value))

    def end_headers(self):
        pass

    def write(self, data):
        self.body.extend(data)

    def flush(self):
        pass
