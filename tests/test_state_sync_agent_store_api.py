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

_LEGACY_VERBS = ("ensure_session", "update_session_cwd", "set_auto_title", "update_token_counts")


def _row(db_path, session_id):
    conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    try:
        return conn.execute("SELECT * FROM sessions WHERE id = ?", (session_id,)).fetchone()
    finally:
        conn.close()


# ── Agent-free: drift is visible once, never raised ─────────────────────────


def test_no_store_api_logs_one_warning_and_returns_none(monkeypatch, tmp_path, caplog):
    fake = types.ModuleType("hermes_state")
    fake.SessionDB = type("SessionDB", (), {"get_session": lambda self, sid: None})
    monkeypatch.setitem(sys.modules, "hermes_state", fake)
    monkeypatch.setitem(sys.modules, "persistence_assembly", None)  # import raises ImportError
    monkeypatch.setattr(state_sync, "_DRIFT_WARNED", set())
    with caplog.at_level(logging.DEBUG, logger="api.state_sync"):
        assert state_sync._open_state_db(tmp_path / "state.db") is None
        assert state_sync._open_state_db(tmp_path / "state.db") is None
    warnings = [r for r in caplog.records if r.levelno == logging.WARNING]
    assert len(warnings) == 1
    assert "persistence_assembly is missing" in warnings[0].getMessage()


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
    db = state_sync._open_state_db(tmp_path / "state.db")
    if db is None:
        pytest.skip("no hermes-agent state store API importable")
    try:
        try:
            from hermes_state import SessionDB
        except ImportError:
            SessionDB = None
        legacy = SessionDB is not None and all(hasattr(SessionDB, v) for v in _LEGACY_VERBS)
        assert isinstance(db, state_sync._StoreStateDB) is (not legacy)
        if not legacy:
            pytest.importorskip("persistence_assembly")
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
    probe = state_sync._open_state_db(db_path)
    try:
        return isinstance(probe, state_sync._StoreStateDB)
    finally:
        if probe is not None:
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


def test_routes_title_sync_passes_rename_intent_only_for_a_user_rename(monkeypatch):
    import api.routes as routes
    from api.session_ops import apply_session_title_rename, mark_session_title_generated

    seen = []
    monkeypatch.setattr(routes, "load_settings", lambda: {"sync_to_insights": True})
    monkeypatch.setattr(state_sync, "sync_session_usage", lambda **kw: seen.append((kw["title"], kw["rename"])))
    session = types.SimpleNamespace(session_id="sid-r", title="Untitled", messages=[], profile="default")

    apply_session_title_rename(session, "My chosen name")  # /api/session/rename
    routes._sync_session_title_to_insights(session)
    session.title = "Generated name"  # _persist_generated_session_title (regenerate / imported)
    mark_session_title_generated(session)
    routes._sync_session_title_to_insights(session)
    apply_session_title_rename(session, "Untitled")  # clearing back to an automatic label
    routes._sync_session_title_to_insights(session)

    assert seen == [("My chosen name", True), ("Generated name", False), ("Untitled", False)]
