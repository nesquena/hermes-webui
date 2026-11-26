"""WebUI's state.db mirror on both Agent store APIs (state_sync._open_state_db).

Agents whose ``hermes_state.SessionDB`` carries ``ensure_session`` / ``update_session_cwd`` /
``set_auto_title`` / ``update_token_counts`` are written through it. Newer Agents ship
``hermes_state`` as a plugin stand-in without those verbs; WebUI writes through the storage
interface (``persistence_assembly``) there, and leaves token and message counts to the Agent,
which records them itself for the turns it runs. The real-Agent test below pins that
premise: if an Agent stops recording usage for a WebUI-shaped turn, it fails.
"""

import json
import logging
import socket
import sqlite3
import sys
import threading
import time
import types
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

from api import state_sync
from tests._agent_state_store import expected_store_is_storage_interface, open_agent_store


def _row(db_path, session_id):
    conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    try:
        return conn.execute("SELECT * FROM sessions WHERE id = ?", (session_id,)).fetchone()
    finally:
        conn.close()


# ── Agent-free: drift is visible once, never raised ─────────────────────────


class _PreFloorSessionDB:
    """An older Agent's SessionDB (v2026.4.30..v2026.7.30 shape): no set_auto_title, no storage interface."""

    instances: list = []

    def __init__(self, db_path):
        self.db_path, self.calls, self.closed = db_path, [], False
        _PreFloorSessionDB.instances.append(self)

    def ensure_session(self, session_id, source="unknown", model=None):
        self.calls.append(("ensure_session", session_id, source))

    def update_token_counts(self, session_id, **kw):
        self.calls.append(("update_token_counts", session_id, kw["input_tokens"], kw["absolute"]))

    def set_session_title(self, session_id, title):
        self.calls.append(("set_session_title", session_id, title))

    def _execute_write(self, fn):
        self.calls.append(("_execute_write",))

    def get_session(self, session_id):
        return {"id": session_id, "source": "webui", "cwd": None}

    def update_session_cwd(self, session_id, cwd):
        self.calls.append(("update_session_cwd", session_id, cwd))
        return 1

    def close(self):
        self.closed = True


@pytest.fixture
def pre_floor_agent(monkeypatch, tmp_path):
    fake = types.ModuleType("hermes_state")
    fake.SessionDB = _PreFloorSessionDB
    monkeypatch.setitem(sys.modules, "hermes_state", fake)
    monkeypatch.setitem(sys.modules, "persistence_assembly", None)  # import raises ImportError
    monkeypatch.setattr(_PreFloorSessionDB, "instances", [])
    monkeypatch.setattr(state_sync, "_DRIFT_WARNED", set())
    monkeypatch.setattr(state_sync, "_get_state_db", lambda profile=None: state_sync._open_state_db(tmp_path / "state.db"))
    return _PreFloorSessionDB.instances


def test_pre_storage_interface_session_db_without_every_verb_is_used_as_before(pre_floor_agent, tmp_path):
    """No persistence_assembly and a SessionDB lacking set_auto_title: used exactly as before this
    bridge learned the storage interface (totals, message count, title, cwd all written)."""
    db = state_sync._open_state_db(tmp_path / "state.db")
    assert isinstance(db, _PreFloorSessionDB)
    state_sync.sync_session_usage("sid-old", input_tokens=7, title="Untitled", message_count=3, profile="default")
    assert pre_floor_agent[-1].calls == [
        ("ensure_session", "sid-old", "webui"), ("update_token_counts", "sid-old", 7, True),
        ("set_session_title", "sid-old", "Untitled"), ("_execute_write",),
    ]
    assert state_sync.sync_session_cwd("sid-old", "/w/a", profile="default") is True
    assert pre_floor_agent[-1].calls[-1] == ("update_session_cwd", "sid-old", "/w/a")


def test_a_verb_the_old_session_db_lacks_logs_one_warning(pre_floor_agent, caplog):
    with caplog.at_level(logging.DEBUG, logger="api.state_sync"):
        state_sync.sync_session_title("sid-a", "A title", profile="default")
        state_sync.sync_session_title("sid-b", "B title", profile="default")
    warnings = [r for r in caplog.records if r.levelno == logging.WARNING]
    assert len(warnings) == 1 and "set_auto_title" in warnings[0].getMessage()
    assert all(db.closed for db in pre_floor_agent) and len(pre_floor_agent) == 2


def test_no_agent_store_api_at_all_returns_none_quietly(monkeypatch, tmp_path, caplog):
    monkeypatch.setitem(sys.modules, "hermes_state", None)
    monkeypatch.setitem(sys.modules, "persistence_assembly", None)
    with caplog.at_level(logging.DEBUG, logger="api.state_sync"):
        assert state_sync._open_state_db(tmp_path / "state.db") is None
    assert not [r for r in caplog.records if r.levelno >= logging.WARNING]


def test_api_shape_error_in_a_sync_warns_once_then_debug(monkeypatch, caplog):
    class _NoTitleVerb:
        closed = 0

        def ensure_session(self, **_kw):
            return None

        def close(self):
            self.closed += 1

    db = _NoTitleVerb()
    monkeypatch.setattr(state_sync, "_get_state_db", lambda profile=None: db)
    monkeypatch.setattr(state_sync, "_DRIFT_WARNED", set())
    with caplog.at_level(logging.DEBUG, logger="api.state_sync"):
        state_sync.sync_session_title("sid-a", "A title", profile="default")
        state_sync.sync_session_title("sid-b", "B title", profile="default")
    warnings = [r for r in caplog.records if r.levelno == logging.WARNING]
    assert len(warnings) == 1 and "AttributeError" in warnings[0].getMessage()
    assert db.closed == 2


# ── real Agent ──────────────────────────────────────────────────────────────


@pytest.mark.requires_agent_modules
def test_open_state_db_picks_the_store_api_this_agent_has(tmp_path):
    """The expected API comes from what is installed (see expected_store_is_storage_interface); an
    installed Agent whose store does not open fails here instead of skipping."""
    db = open_agent_store(tmp_path / "state.db")
    try:
        storage = expected_store_is_storage_interface()
        assert isinstance(db, state_sync._StoreStateDB) is storage
        if not storage:
            from hermes_state import SessionDB

            assert isinstance(db, SessionDB)
    finally:
        db.close()


class _MockChat(BaseHTTPRequestHandler):
    """OpenAI-compatible chat completions with fixed usage (prompt 11, completion 7)."""

    def log_message(self, *_a):
        pass

    def do_GET(self):
        self._json({"object": "list", "data": [{"id": "probe-model", "object": "model"}]})

    def do_POST(self):
        req = json.loads(self.rfile.read(int(self.headers.get("Content-Length") or 0)) or b"{}")
        usage = {"prompt_tokens": 11, "completion_tokens": 7, "total_tokens": 18}
        base = {"id": "c1", "created": int(time.time()), "model": "probe-model"}
        if not req.get("stream"):
            self._json({**base, "object": "chat.completion", "usage": usage, "choices": [
                {"index": 0, "message": {"role": "assistant", "content": "Agent reply"}, "finish_reason": "stop"}]})
            return
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.end_headers()
        base["object"] = "chat.completion.chunk"
        for chunk in (
            {**base, "choices": [{"index": 0, "delta": {"role": "assistant", "content": "Agent reply"}, "finish_reason": None}]},
            {**base, "choices": [{"index": 0, "delta": {}, "finish_reason": "stop"}]},
            {**base, "choices": [], "usage": usage},
        ):
            self.wfile.write(f"data: {json.dumps(chunk)}\n\n".encode())
        self.wfile.write(b"data: [DONE]\n\n")

    def _json(self, payload):
        body = json.dumps(payload).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


def _mock_server():
    for port in range(18940, 18960):
        with socket.socket() as probe:
            if probe.connect_ex(("127.0.0.1", port)) == 0:
                continue
        try:
            server = ThreadingHTTPServer(("127.0.0.1", port), _MockChat)
        except OSError:
            continue
        threading.Thread(target=server.serve_forever, daemon=True).start()
        return server
    pytest.skip("no free port in 18940-18959 for the mock chat server")


def _run_webui_turns(tmp_path, sid):
    """Two WebUI-shaped AIAgent turns (platform='webui', session_db=SessionDB(path)) against the mock
    server; returns the state.db path. The Agent's own aux title call names the row ('Agent reply', llm)."""
    run_agent = pytest.importorskip("run_agent")
    hermes_state = pytest.importorskip("hermes_state")
    db_path = tmp_path / "state.db"
    server = _mock_server()
    session_db = hermes_state.SessionDB(db_path=db_path)
    try:
        agent = run_agent.AIAgent(
            model="probe-model", provider="custom", api_key="dummy-key",
            base_url=f"http://127.0.0.1:{server.server_address[1]}/v1",
            platform="webui", quiet_mode=True, enabled_toolsets=[], session_id=sid, session_db=session_db,
            stream_delta_callback=lambda *_a, **_k: None,
        )
        agent.run_conversation("first turn")
        agent.run_conversation("second turn")
        close = getattr(agent, "close", None)
        if callable(close):
            close()
    finally:
        session_db.close()
        server.shutdown()
    return db_path


def _storage_api(db_path):
    probe = open_agent_store(db_path)
    try:
        return isinstance(probe, state_sync._StoreStateDB)
    finally:
        probe.close()


@pytest.mark.requires_agent_modules
def test_agent_records_usage_for_a_webui_turn_and_the_mirror_adds_cwd(tmp_path, monkeypatch):
    """A WebUI-shaped AIAgent turn (platform='webui', session_db=SessionDB(path)) writes the row,
    token counts, call count and message count itself on every supported Agent; WebUI's mirror
    adds the workspace cwd, and on storage-interface Agents leaves the Agent's usage untouched."""
    sid = "webui-real-turn"
    db_path = _run_webui_turns(tmp_path, sid)

    agent_row = _row(db_path, sid)
    assert agent_row is not None and agent_row["source"] == "webui"
    assert (agent_row["input_tokens"], agent_row["output_tokens"], agent_row["api_call_count"]) == (22, 14, 2)
    assert agent_row["message_count"] == 4
    assert not agent_row["cwd"]  # the Agent stamps cwd for CLI sources only

    monkeypatch.setattr(state_sync, "_get_state_db", lambda profile=None: state_sync._open_state_db(db_path))
    workspace = str(tmp_path / "workspace")
    assert state_sync.sync_session_cwd(sid, workspace + "/", profile="default") is True
    state_sync.sync_session_usage(
        sid, input_tokens=999, output_tokens=888, model="probe-model", message_count=40, profile="default",
    )

    row = _row(db_path, sid)
    assert row["cwd"] == workspace
    if _storage_api(db_path):
        assert (row["input_tokens"], row["output_tokens"], row["message_count"]) == (22, 14, 4)
    else:  # legacy SessionDB: WebUI's absolute totals, as before
        assert (row["input_tokens"], row["output_tokens"], row["message_count"]) == (999, 888, 40)


@pytest.mark.requires_agent_modules
def test_passive_usage_sync_keeps_the_agents_generated_title_and_a_rename_replaces_it(tmp_path, monkeypatch):
    """Storage-interface Agents: the turn-end usage sync carries WebUI's provisional title, which must
    not replace the Agent's generated ('llm') title or take user provenance; an explicit rename must."""
    sid = "webui-real-title"
    db_path = _run_webui_turns(tmp_path, sid)
    if not _storage_api(db_path):
        pytest.skip("legacy SessionDB Agent: usage sync keeps writing the title as before")
    assert (_row(db_path, sid)["title"], _row(db_path, sid)["title_source"]) == ("Agent reply", "llm")
    monkeypatch.setattr(state_sync, "_get_state_db", lambda profile=None: state_sync._open_state_db(db_path))

    state_sync.sync_session_usage(sid, model="probe-model", title="Provisional title", profile="default")
    row = _row(db_path, sid)
    assert (row["title"], row["title_source"]) == ("Agent reply", "llm")

    state_sync.sync_session_usage(sid, model="probe-model", title="Renamed by user", profile="default", rename=True)
    row = _row(db_path, sid)
    assert (row["title"], row["title_source"]) == ("Renamed by user", "user")


@pytest.mark.requires_agent_modules
def test_renaming_back_to_an_automatic_label_releases_the_title_for_auto_titling(tmp_path, monkeypatch):
    """Storage-interface Agents: a user rename pins a 'user' title; renaming back to an empty or
    automatic label must clear it (title and provenance), so a later generated title can land."""
    sid = "webui-real-clear"
    db_path = _run_webui_turns(tmp_path, sid)
    if not _storage_api(db_path):
        pytest.skip("legacy SessionDB Agent: usage sync keeps writing the title as before")
    monkeypatch.setattr(state_sync, "_get_state_db", lambda profile=None: state_sync._open_state_db(db_path))

    for cleared_label in ("Untitled", ""):
        state_sync.sync_session_usage(sid, title="Foo", profile="default", rename=True)
        assert (_row(db_path, sid)["title"], _row(db_path, sid)["title_source"]) == ("Foo", "user")
        state_sync.sync_session_usage(sid, title=cleared_label, profile="default", rename=True)
        assert (_row(db_path, sid)["title"], _row(db_path, sid)["title_source"]) == (None, None)

    state_sync.sync_session_title(sid, "Generated later", profile="default")
    assert (_row(db_path, sid)["title"], _row(db_path, sid)["title_source"]) == ("Generated later", "llm")


class _RecordingSessions:
    def __init__(self):
        self.calls = []

    def create(self, spec):
        self.calls.append(("create", spec.session_id))
        return spec.session_id

    def set_title(self, session_id, title):
        self.calls.append(("set_title", session_id, title))
        return True


def test_storage_interface_usage_sync_writes_the_title_only_for_a_rename(monkeypatch):
    """Stand-in store handle and NewSession, so this runs without an Agent."""
    sessions_mod = types.ModuleType("persistence.conversation.sessions")
    sessions_mod.NewSession = lambda **kw: types.SimpleNamespace(**kw)
    monkeypatch.setitem(sys.modules, "persistence.conversation.sessions", sessions_mod)
    sessions = _RecordingSessions()
    handle = types.SimpleNamespace(sessions=sessions, close=lambda: None)
    monkeypatch.setattr(state_sync, "_get_state_db", lambda profile=None: state_sync._StoreStateDB(handle))

    state_sync.sync_session_usage("sid-1", input_tokens=5, title="Provisional", message_count=2, profile="default")
    assert sessions.calls == [("create", "sid-1")]

    state_sync.sync_session_usage("sid-1", title="Chosen name", profile="default", rename=True)
    assert sessions.calls[1:] == [("create", "sid-1"), ("set_title", "sid-1", "Chosen name")]

    del sessions.calls[:]
    for label in ("Untitled", " new chat ", "", None):  # a rename back to an automatic label clears
        state_sync.sync_session_usage("sid-1", title=label, profile="default", rename=True)
    assert [c for c in sessions.calls if c[0] == "set_title"] == [("set_title", "sid-1", None)] * 4


def test_legacy_session_db_title_writes_are_unchanged_by_rename_intent(monkeypatch):
    """Legacy SessionDB: the title is written whenever given, rename or not, as before (no clearing)."""
    class _LegacyDB(_PreFloorSessionDB):
        def set_auto_title(self, *a, **k):
            return True

    written = []
    monkeypatch.setattr(_PreFloorSessionDB, "instances", [])
    monkeypatch.setattr(state_sync, "_get_state_db", lambda profile=None: _LegacyDB(None))
    monkeypatch.setattr(_LegacyDB, "set_session_title", lambda self, sid, title: written.append(title))
    for title, rename in (("Provisional", False), ("Untitled", False), ("Untitled", True), ("", True), ("Mine", True)):
        state_sync.sync_session_usage("sid-l", title=title, profile="default", rename=rename)
    assert written == ["Provisional", "Untitled", "Untitled", "Mine"]


def test_routes_title_sync_forwards_the_call_sites_rename_intent(monkeypatch):
    """Rename intent comes from the call site (/api/session/rename passes rename=True), not from
    session flags: a copied ``manual_title`` (e.g. a duplicate) does not make a sync a rename."""
    import api.routes as routes

    seen = []
    monkeypatch.setattr(routes, "load_settings", lambda: {"sync_to_insights": True})
    monkeypatch.setattr(state_sync, "sync_session_usage", lambda **kw: seen.append((kw["title"], kw["rename"])))
    session = types.SimpleNamespace(session_id="sid-r", title="My name", messages=[], profile="default",
                                    manual_title=True)

    routes._sync_session_title_to_insights(session, rename=True)  # /api/session/rename
    routes._sync_session_title_to_insights(session)  # _persist_generated_session_title and any other caller
    session.title = "Untitled"
    routes._sync_session_title_to_insights(session, rename=True)  # rename back to an automatic label

    assert seen == [("My name", True), ("My name", False), ("Untitled", True)]


# ── Title provenance: local fallback (Refs #9157) ───────────────────────────


def _new_agent_store(db_path):
    """Create an empty state.db on the installed Agent; skip when its store has no automatic-title verb."""
    hermes_state = pytest.importorskip("hermes_state")
    hermes_state.SessionDB(db_path=db_path).close()
    probe = open_agent_store(db_path)
    try:
        if not hasattr(probe, "set_auto_title"):
            pytest.skip("this Agent's store has no automatic-title verb (before v2026.8.13)")
        return isinstance(probe, state_sync._StoreStateDB)
    finally:
        probe.close()


@pytest.mark.requires_agent_modules
def test_a_local_fallback_title_is_derived_so_the_agents_model_title_replaces_it(tmp_path, monkeypatch):
    """WebUI's local fallback title is stored with 'derived' provenance, so the Agent's own model
    title ('llm') can still replace it on every Agent with an automatic-title verb."""
    db_path = tmp_path / "state.db"
    _new_agent_store(db_path)
    monkeypatch.setattr(state_sync, "_get_state_db", lambda profile=None: state_sync._open_state_db(db_path))
    sid = "webui-fallback-title"

    state_sync.sync_session_title(sid, "Plan the billing database migration", profile="default", source="derived")
    row = _row(db_path, sid)
    assert (row["title"], row["title_source"]) == ("Plan the billing database migration", "derived")

    agent_store = state_sync._open_state_db(db_path)
    try:
        agent_store.set_auto_title(sid, "Billing migration plan", source=getattr(agent_store, "TITLE_SOURCE_LLM", "llm"))
    finally:
        agent_store.close()
    row = _row(db_path, sid)
    assert (row["title"], row["title_source"]) == ("Billing migration plan", "llm")


@pytest.mark.parametrize(
    ("model_title", "expected_source"),
    [(None, "derived"), ("Billing migration plan", "llm")],
    ids=["local-fallback", "model-title"],
)
def test_background_title_syncs_a_local_fallback_as_derived(monkeypatch, model_title, expected_source):
    """The background title path passes 'derived' for its local fallback and 'llm' for a model title."""
    from unittest.mock import MagicMock

    import api.streaming as streaming

    user_text = "Help me plan the database migration for the billing service"
    assistant_text = "Sure. Start by listing every table the billing service writes to."
    session = MagicMock()
    session.title = "Untitled"
    session.llm_title_generated = False
    session.manual_title = False
    session.messages = [{"role": "user", "content": user_text}, {"role": "assistant", "content": assistant_text}]
    monkeypatch.setattr(streaming, "get_session", lambda *_a, **_k: session)
    monkeypatch.setattr(streaming, "_aux_title_configured", lambda *_a, **_k: True)
    status = "llm" if model_title else "llm_length_aux"
    monkeypatch.setattr(streaming, "_generate_llm_session_title_via_aux", lambda *_a, **_k: (model_title, status, ""))
    synced = []
    monkeypatch.setattr(
        state_sync, "sync_session_title",
        lambda sid, title, profile=None, **kw: synced.append((title, kw.get("source", "llm"))),
    )
    events = []

    streaming._run_background_title_update(
        session_id="sid-title-source", user_text=user_text, assistant_text=assistant_text,
        placeholder_title="Untitled", put_event=lambda kind, data: events.append((kind, data)), agent=None,
    )

    statuses = [data["status"] for kind, data in events if kind == "title_status"]
    assert statuses == (["fallback"] if model_title is None else [status])
    assert [source for _title, source in synced] == [expected_source]
