"""WebUI sessions must record their workspace as ``sessions.cwd`` in state.db.

The Agent creates the state.db row lazily during ``run_conversation()``, but
``run_agent._launch_cwd_for_session`` only stamps ``cwd`` for CLI-family
sources. WebUI rows therefore kept an empty ``cwd`` and Hermes Desktop, which
groups sessions by ``cwd``/``git_repo_root``, filed them under "Home" instead
of the workspace they were created in.

``_FakeSessionDB`` mirrors the three SessionDB methods the helper uses, with
the Agent's ``update_session_cwd`` semantics (row must exist, the generation
is bumped on every write, git metadata is cleared on a cwd change), so the
behavioural tests run in CI where hermes-agent is not installed. The last
test repeats the core check against the real SessionDB when it is importable.
"""

from __future__ import annotations

import queue
import sys
import types
from unittest import mock
from urllib.parse import urlparse

import pytest

import api.config as config
import api.models as models
import api.state_sync as state_sync
import api.streaming as streaming
from api.models import Session


class _FakeSessionDB:
    """In-memory stand-in for hermes_state.SessionDB (the subset used here)."""

    def __init__(self):
        self.rows: dict[str, dict] = {}
        self.closed = False
        self.cwd_writes = 0

    def create_session(self, session_id, source="webui", cwd=None, **_kw):
        self.rows.setdefault(session_id, {
            "id": session_id, "source": source, "cwd": cwd,
            "git_branch": None, "git_repo_root": None, "git_metadata_generation": 0,
        })

    def get_session(self, session_id):
        row = self.rows.get(session_id)
        return dict(row) if row else None

    def update_session_cwd(self, session_id, cwd, git_branch=None, git_repo_root=None, replace_git_meta=False):
        row = self.rows.get(session_id)
        if not session_id or not cwd or row is None:
            return None
        if row["cwd"] != cwd or replace_git_meta:
            row["git_branch"] = git_branch or None
            row["git_repo_root"] = git_repo_root or None
        row["cwd"] = cwd
        row["git_metadata_generation"] += 1
        self.cwd_writes += 1
        return row["git_metadata_generation"]

    def close(self):
        self.closed = True


@pytest.fixture
def fake_db(monkeypatch):
    db = _FakeSessionDB()
    monkeypatch.setattr(state_sync, "_get_state_db", lambda profile=None: db)
    return db


# ── helper contract ─────────────────────────────────────────────────────────


def test_fills_empty_cwd_on_agent_created_row(fake_db):
    fake_db.create_session("sess-ws", source="webui", cwd=None)
    assert state_sync.sync_session_cwd("sess-ws", "/home/u/workspace", profile="default") is True
    assert fake_db.rows["sess-ws"]["cwd"] == "/home/u/workspace"
    assert fake_db.closed is True  # handle it opened is released


def test_never_creates_a_row(fake_db):
    """A session that never sent a message must stay out of state.db."""
    assert state_sync.sync_session_cwd("sess-empty", "/home/u/workspace") is False
    assert fake_db.rows == {}


def test_workspace_change_moves_row_and_clears_stale_git_meta(fake_db):
    fake_db.create_session("sess-move")
    fake_db.update_session_cwd("sess-move", "/repo/a", git_branch="main", git_repo_root="/repo/a")
    assert state_sync.sync_session_cwd("sess-move", "/home/u/other") is True
    row = fake_db.rows["sess-move"]
    assert row["cwd"] == "/home/u/other"
    assert row["git_repo_root"] is None and row["git_branch"] is None


def test_idempotent_and_trailing_slash_normalised(fake_db):
    fake_db.create_session("sess-slash")
    assert state_sync.sync_session_cwd("sess-slash", "/home/u/customers/acme/") is True
    assert fake_db.rows["sess-slash"]["cwd"] == "/home/u/customers/acme"
    generation = fake_db.rows["sess-slash"]["git_metadata_generation"]
    assert state_sync.sync_session_cwd("sess-slash", "/home/u/customers/acme") is False
    assert state_sync.sync_session_cwd("sess-slash", "/home/u/customers/acme/") is False
    assert fake_db.rows["sess-slash"]["git_metadata_generation"] == generation


def test_root_workspace_is_kept(fake_db):
    fake_db.create_session("sess-root")
    assert state_sync.sync_session_cwd("sess-root", "/") is True
    assert fake_db.rows["sess-root"]["cwd"] == "/"


@pytest.mark.parametrize("anchor", ["C:\\", "C:/", "D:\\", "\\\\host\\share\\", "\\\\host\\share"])
def test_windows_anchors_are_preserved(anchor):
    """``C:`` is drive-relative and ``\\\\host`` is not a share: never strip an anchor."""
    assert state_sync._normalize_session_cwd(anchor) == anchor


@pytest.mark.parametrize("raw, expected", [
    ("C:\\work\\proj\\", "C:\\work\\proj"),
    ("\\\\host\\share\\proj\\", "\\\\host\\share\\proj"),
    ("/home/u/proj/", "/home/u/proj"),
    ("/home/u/proj//", "/home/u/proj"),
])
def test_trailing_separator_stripped_below_the_anchor(raw, expected):
    assert state_sync._normalize_session_cwd(raw) == expected


def test_reuses_caller_db_without_closing_it(monkeypatch):
    """The streaming path passes the Agent's own profile-bound SessionDB."""
    monkeypatch.setattr(
        state_sync, "_get_state_db",
        lambda profile=None: pytest.fail("must not open a second handle when db= is given"),
    )
    db = _FakeSessionDB()
    db.create_session("sess-shared")
    assert state_sync.sync_session_cwd("sess-shared", "/home/u/workspace", db=db) is True
    assert db.closed is False


def test_blank_input_missing_db_and_db_errors_are_swallowed(monkeypatch):
    monkeypatch.setattr(state_sync, "_get_state_db", lambda profile=None: None)
    assert state_sync.sync_session_cwd("", "/x") is False
    assert state_sync.sync_session_cwd("sid", "") is False
    assert state_sync.sync_session_cwd("sid", None) is False
    assert state_sync.sync_session_cwd("sid", "/x") is False

    broken = _FakeSessionDB()
    broken.create_session("sid")
    broken.update_session_cwd = mock.Mock(side_effect=RuntimeError("database is locked"))
    assert state_sync.sync_session_cwd("sid", "/x", db=broken) is False


# ── only rows the WebUI owns ────────────────────────────────────────────────


@pytest.mark.parametrize("source", ["cli", "tui", "desktop", "telegram", "cron", "subagent"])
def test_foreign_source_rows_are_never_touched(fake_db, source):
    """Opening an imported session must not overwrite its own cwd / git identity."""
    fake_db.create_session("foreign", source=source)
    fake_db.update_session_cwd("foreign", "/projects/own", git_branch="main", git_repo_root="/projects/own")
    generation = fake_db.rows["foreign"]["git_metadata_generation"]
    assert state_sync.sync_session_cwd("foreign", "/home/u/workspace") is False
    row = fake_db.rows["foreign"]
    assert (row["cwd"], row["git_branch"], row["git_repo_root"]) == ("/projects/own", "main", "/projects/own")
    assert row["git_metadata_generation"] == generation


# ── never make the caller wait on a busy state.db ───────────────────────────


def test_background_sync_returns_immediately_while_the_db_is_busy(monkeypatch):
    """SessionDB retries writes for ~20 s under contention. Stream cleanup and
    the workspace-update response must not wait for that."""
    import threading
    import time

    gate = threading.Event()

    class _BusyDB(_FakeSessionDB):
        def update_session_cwd(self, *a, **k):
            gate.wait(10)  # a writer holds the lock
            return super().update_session_cwd(*a, **k)

    db = _BusyDB()
    db.create_session("busy")
    monkeypatch.setattr(state_sync, "_get_state_db", lambda profile=None: db)

    started = time.monotonic()
    state_sync.sync_session_cwd_background(lambda: ("busy", "/home/u/workspace", None))
    assert time.monotonic() - started < 1.0, "the caller waited for the busy database"
    assert db.rows["busy"]["cwd"] is None  # not written yet
    gate.set()
    assert state_sync.drain_cwd_syncs()
    assert db.rows["busy"]["cwd"] == "/home/u/workspace"


def test_background_syncs_converge_on_the_latest_workspace(fake_db):
    """Each write re-reads the current workspace, so overlapping syncs end on
    the newest value whatever order they run in."""
    fake_db.create_session("race")
    current = {"ws": "/home/u/a"}
    state_sync.sync_session_cwd_background(lambda: ("race", current["ws"], None))
    current["ws"] = "/home/u/b"
    state_sync.sync_session_cwd_background(lambda: ("race", current["ws"], None))
    assert state_sync.drain_cwd_syncs()
    assert fake_db.rows["race"]["cwd"] == "/home/u/b"


def test_background_sync_swallows_resolver_and_db_errors(fake_db):
    def _boom():
        raise RuntimeError("session store unavailable")

    state_sync.sync_session_cwd_background(_boom)
    state_sync.sync_session_cwd_background(lambda: None)
    assert state_sync.drain_cwd_syncs()
    assert fake_db.rows == {}


def test_trailing_whitespace_is_part_of_the_workspace_path(fake_db, tmp_path):
    """``/x/acme `` (trailing space) is a different directory from ``/x/acme``."""
    plain, spaced = tmp_path / "acme", tmp_path / "acme "
    plain.mkdir()
    spaced.mkdir()
    assert state_sync._normalize_session_cwd(str(spaced)) == str(spaced)
    assert state_sync._normalize_session_cwd(f"  {plain}/ ") == f"  {plain}/ ".rstrip("/\\")
    assert state_sync._normalize_session_cwd("   ") == ""  # blank is still blank
    fake_db.create_session("ws-space", cwd=str(plain))
    assert state_sync.sync_session_cwd("ws-space", str(spaced)) is True
    assert fake_db.rows["ws-space"]["cwd"] == str(spaced)
    assert fake_db.rows["ws-space"]["cwd"] != str(plain)
    assert state_sync.sync_session_cwd("ws-space", str(spaced)) is False  # idempotent


def test_profile_none_session_reads_the_root_state_db(monkeypatch):
    """A legacy ``profile=None`` session lives in the root home. The background
    thread has no request profile, so ``None`` must not fall back to the
    process-active (named) profile's state.db."""
    root_db, active_db = _FakeSessionDB(), _FakeSessionDB()
    root_db.create_session("legacy")
    active_db.create_session("legacy", cwd="/home/u/active")  # same id, other profile
    seen = []

    def _get_state_db(profile=None):
        seen.append(profile)
        # ``None`` resolves to the process-active named profile, as in production.
        return root_db if profile == "default" else active_db

    monkeypatch.setattr(state_sync, "_get_state_db", _get_state_db)
    state_sync.sync_session_cwd_background(lambda: ("legacy", "/home/u/workspace", None))
    assert state_sync.drain_cwd_syncs()
    assert seen == ["default"]
    assert root_db.rows["legacy"]["cwd"] == "/home/u/workspace"
    assert active_db.rows["legacy"]["cwd"] == "/home/u/active"
    assert active_db.cwd_writes == 0


# ── streaming worker: every exit path ───────────────────────────────────────


@pytest.fixture
def stream_env(tmp_path, monkeypatch):
    session_dir = tmp_path / "sessions"
    session_dir.mkdir()
    monkeypatch.setattr(models, "SESSION_DIR", session_dir)
    monkeypatch.setattr(models, "SESSION_INDEX_FILE", session_dir / "_index.json")
    monkeypatch.setattr(streaming, "_attempt_credential_self_heal", lambda *a, **k: None)
    for registry in (config.STREAMS, config.CANCEL_FLAGS, config.AGENT_INSTANCES,
                     config.STREAM_PARTIAL_TEXT, config.SESSION_AGENT_LOCKS, models.SESSIONS):
        registry.clear()

    fake_runtime = types.ModuleType("hermes_cli.runtime_provider")
    fake_runtime.resolve_runtime_provider = lambda requested=None, **_kw: {
        "provider": requested or "test-provider", "api_key": "synthetic-key", "base_url": None,
    }
    fake_cli = types.ModuleType("hermes_cli")
    fake_cli.runtime_provider = fake_runtime
    fake_state = types.ModuleType("hermes_state")
    fake_state.SessionDB = mock.Mock(return_value=None)
    monkeypatch.setitem(sys.modules, "hermes_cli", fake_cli)
    monkeypatch.setitem(sys.modules, "hermes_cli.runtime_provider", fake_runtime)
    monkeypatch.setitem(sys.modules, "hermes_state", fake_state)

    workspace = tmp_path / "ws" / "project"
    workspace.mkdir(parents=True)
    yield workspace
    for registry in (config.STREAMS, config.CANCEL_FLAGS, config.AGENT_INSTANCES,
                     config.STREAM_PARTIAL_TEXT, config.SESSION_AGENT_LOCKS, models.SESSIONS):
        registry.clear()


def _agent_class(db, outcome):
    """An Agent double that, like the real one, creates the row with cwd=None."""

    class _Agent:
        def __init__(self, **kwargs):
            self.session_id = kwargs.get("session_id")
            self._session_db = db
            self.stream_delta_callback = kwargs.get("stream_delta_callback")
            self.session_prompt_tokens = 0
            self.session_completion_tokens = 0
            self.session_estimated_cost_usd = 0.0
            self.context_compressor = None
            self._last_error = None
            self.ephemeral_system_prompt = None

        def run_conversation(self, **kwargs):
            db.create_session(self.session_id, source="webui", cwd=None)
            history = list(kwargs.get("conversation_history") or [])
            if outcome == "raise":
                raise RuntimeError("provider exploded")
            if outcome == "error":
                return {"messages": history, "error": {"error": {
                    "type": "authentication_error", "status_code": 401,
                    "code": "auth_unavailable", "message": "token invalidated"}}}
            if outcome == "cancel":
                config.CANCEL_FLAGS[self._stream_id].set()
            return {"status": "ok",
                    "messages": history + [{"role": "assistant", "content": "hello"}]}

        def interrupt(self, _message):
            pass

    return _Agent


def _run_stream(db, workspace, outcome, sid, *, current_session=None, during_turn=None):
    """Drive the real worker. ``current_session`` is what ``get_session()``
    resolves once ``during_turn`` ran (a detached-snapshot race); by default
    it is the worker's own session object."""
    stream_id = f"stream-{sid}"
    session = Session(session_id=sid, title="t")
    session.messages, session.context_messages = [], []
    session.pending_user_message = "hi"
    session.pending_started_at = 1.0
    session.active_stream_id = stream_id
    session.save()
    models.SESSIONS[sid] = session
    config.STREAMS[stream_id] = queue.Queue()
    config.STREAM_PARTIAL_TEXT[stream_id] = ""
    holder = {"current": session}

    agent_cls = _agent_class(db, outcome)
    original_init = agent_cls.__init__
    original_run = agent_cls.run_conversation

    def _init(self, **kwargs):
        original_init(self, **kwargs)
        self._stream_id = stream_id

    def _run(self, **kwargs):
        try:
            return original_run(self, **kwargs)
        finally:
            if during_turn is not None:
                during_turn(session)
            if current_session is not None:
                holder["current"] = current_session

    def _get_session(_sid, *a, **k):
        cur = holder["current"]
        if isinstance(cur, Exception):
            raise cur
        return cur

    agent_cls.__init__ = _init
    agent_cls.run_conversation = _run
    with mock.patch.object(streaming, "get_session", side_effect=_get_session), \
         mock.patch.object(streaming, "_get_ai_agent", return_value=agent_cls), \
         mock.patch.object(streaming, "resolve_model_provider", return_value=("test-model", "test-provider", None)), \
         mock.patch("api.config.get_config", return_value={}), \
         mock.patch("api.config._resolve_cli_toolsets", return_value=[]):
        streaming._run_agent_streaming(
            session_id=sid, msg_text="hi", model="test-model",
            workspace=str(workspace), stream_id=stream_id,
        )
        # The write runs in the background and resolves the current session
        # when it executes, so wait for it while get_session() is still patched.
        assert state_sync.drain_cwd_syncs(), "background cwd sync did not finish"


@pytest.mark.parametrize("outcome", ["success", "error", "raise", "cancel"])
def test_streaming_turn_records_workspace_on_every_exit(stream_env, monkeypatch, outcome):
    """The row the Agent created must carry the workspace however the turn ends."""
    db = _FakeSessionDB()
    # The background write opens the session's profile DB (#2762).
    monkeypatch.setattr(state_sync, "_get_state_db", lambda profile=None: db)
    sid = f"stream-{outcome}"
    _run_stream(db, stream_env, outcome, sid)
    assert sid in db.rows, "the Agent double should have created the row"
    assert db.rows[sid]["cwd"] == str(stream_env)


@pytest.mark.parametrize("outcome", ["success", "cancel", "raise"])
def test_stale_worker_does_not_overwrite_a_newer_workspace(stream_env, tmp_path, monkeypatch, outcome):
    """Cancel clears ownership early; the canonical session then moves to
    workspace B. The unwinding worker still holds workspace A and must not
    put ``sessions.cwd`` back to A."""
    db = _FakeSessionDB()
    monkeypatch.setattr(state_sync, "_get_state_db", lambda profile=None: db)
    sid = f"stale-{outcome}"
    workspace_b = tmp_path / "ws" / "other"
    workspace_b.mkdir(parents=True)

    current = Session(session_id=sid, title="t")
    current.workspace = str(workspace_b)  # the detached-successor object

    def _move_while_worker_unwinds(worker_session):
        worker_session.active_stream_id = None  # cancel_stream() fence
        db.update_session_cwd(sid, str(workspace_b))  # /api/session/update

    _run_stream(db, stream_env, outcome, sid,
                current_session=current, during_turn=_move_while_worker_unwinds)
    assert db.rows[sid]["cwd"] == str(workspace_b)


def test_unresolvable_current_session_skips_the_write(stream_env, monkeypatch):
    """Fail closed: no current session object, no state.db write from the
    worker-held snapshot."""
    db = _FakeSessionDB()
    monkeypatch.setattr(state_sync, "_get_state_db", lambda profile=None: db)
    _run_stream(db, stream_env, "success", "gone",
                current_session=RuntimeError("session store unavailable"))
    assert db.rows["gone"]["cwd"] is None


# ── synchronous /api/chat and workspace change ──────────────────────────────


def test_sync_chat_records_workspace_even_when_the_turn_raises(tmp_path, monkeypatch, fake_db):
    import api.routes as routes

    workspace = tmp_path / "ws"
    workspace.mkdir()
    session = Session(session_id="sync-sid", title="t")
    session.workspace = str(workspace)

    def _agent_created_row_then_failed():
        fake_db.create_session("sync-sid", source="webui", cwd=None)
        raise RuntimeError("agent unavailable")

    monkeypatch.setattr(routes, "_agent_runtime_barrier_response", lambda **_k: None)
    monkeypatch.setattr(routes, "get_session", lambda _sid: session)
    monkeypatch.setattr(routes, "resolve_trusted_workspace", lambda ws, **_k: ws)
    monkeypatch.setattr(routes, "_read_profile_model_config", lambda *_a, **_k: (None, None, None))
    monkeypatch.setattr(
        routes, "_resolve_compatible_session_model_state",
        lambda model, provider, **_k: (model, provider),
    )
    monkeypatch.setattr(routes, "require_ai_agent_class", _agent_created_row_then_failed)

    with pytest.raises(RuntimeError, match="agent unavailable"):
        routes._handle_chat_sync(object(), {"session_id": "sync-sid", "message": "hi"})
    assert state_sync.drain_cwd_syncs()
    assert fake_db.rows["sync-sid"]["cwd"] == str(workspace)


def test_workspace_update_moves_existing_row(tmp_path, monkeypatch, fake_db):
    import api.routes as routes

    old_ws, new_ws = tmp_path / "a", tmp_path / "b"
    old_ws.mkdir()
    new_ws.mkdir()
    session = Session(session_id="upd-sid", title="t")
    session.workspace = str(old_ws)
    session.save = lambda *a, **k: None
    fake_db.create_session("upd-sid", source="webui", cwd=str(old_ws))

    captured = {}
    monkeypatch.setattr(routes, "_check_csrf", lambda _h: True, raising=False)
    monkeypatch.setattr(routes, "read_body", lambda _h: {"session_id": "upd-sid", "workspace": str(new_ws)})
    monkeypatch.setattr(routes, "_get_or_materialize_session", lambda _sid: session)
    monkeypatch.setattr(routes, "get_session", lambda _sid, **_k: session)
    monkeypatch.setattr(routes, "resolve_trusted_workspace", lambda ws, **_k: ws)
    monkeypatch.setattr(routes, "set_last_workspace", lambda *a, **k: None)
    monkeypatch.setattr(routes, "j", lambda _h, obj, *a, **k: captured.setdefault("ok", obj) or True)
    monkeypatch.setattr(routes, "bad", lambda _h, msg, code=400: captured.setdefault("bad", (msg, code)) or True)

    class _Handler:
        headers = {"Content-Type": "application/json"}
        command = "POST"
        path = "/api/session/update"
        client_address = ("127.0.0.1", 0)

    routes.handle_post(_Handler(), urlparse("/api/session/update"))
    assert state_sync.drain_cwd_syncs()
    assert "bad" not in captured, captured.get("bad")
    assert fake_db.rows["upd-sid"]["cwd"] == str(new_ws)


def test_delayed_workspace_syncs_resolve_the_current_session_not_a_captured_one(
    tmp_path, monkeypatch, fake_db
):
    """Two workspace changes with the cached ``Session`` object replaced between
    them; the callbacks then run in reverse order. Each must resolve the CURRENT
    session by id when it runs, so the row ends on the latest workspace."""
    import api.routes as routes

    ws_a, ws_b, ws_c = (tmp_path / n for n in "abc")
    for d in (ws_a, ws_b, ws_c):
        d.mkdir()

    def _session(workspace):
        s = Session(session_id="swap-sid", title="t")
        s.workspace = str(workspace)
        s.save = lambda *a, **k: None
        return s

    store = {"swap-sid": _session(ws_a)}  # stands in for SESSIONS
    fake_db.create_session("swap-sid", source="webui", cwd=str(ws_a))
    resolvers = []
    body = {}
    monkeypatch.setattr(state_sync, "sync_session_cwd_background", resolvers.append)
    monkeypatch.setattr(routes, "_check_csrf", lambda _h: True, raising=False)
    monkeypatch.setattr(routes, "read_body", lambda _h: dict(body))
    monkeypatch.setattr(routes, "_get_or_materialize_session", lambda sid: store[sid])
    monkeypatch.setattr(routes, "get_session", lambda sid, **_k: store[sid])
    monkeypatch.setattr(routes, "resolve_trusted_workspace", lambda ws, **_k: ws)
    monkeypatch.setattr(routes, "set_last_workspace", lambda *a, **k: None)
    monkeypatch.setattr(routes, "j", lambda _h, obj, *a, **k: True)
    monkeypatch.setattr(routes, "bad", lambda _h, msg, code=400: pytest.fail(msg))

    class _Handler:
        headers = {"Content-Type": "application/json"}
        command = "POST"
        path = "/api/session/update"
        client_address = ("127.0.0.1", 0)

    def _update(workspace):
        body.update({"session_id": "swap-sid", "workspace": str(workspace)})
        routes.handle_post(_Handler(), urlparse("/api/session/update"))

    _update(ws_b)                       # A -> B, schedules callback 1
    store["swap-sid"] = _session(ws_b)  # object replaced (eviction / disk reload)
    _update(ws_c)                       # B -> C, schedules callback 2
    assert len(resolvers) == 2

    for resolve in reversed(resolvers):  # callback 2 first, then the older one
        target = resolve()
        assert target == ("swap-sid", str(ws_c), None)
        state_sync.sync_session_cwd(*target)
    assert fake_db.rows["swap-sid"]["cwd"] == str(ws_c)


def test_delayed_workspace_sync_skips_a_session_that_no_longer_resolves(monkeypatch):
    import api.routes as routes

    def _gone(_sid, **_k):
        raise KeyError(_sid)

    monkeypatch.setattr(routes, "get_session", _gone)
    assert routes._current_cwd_sync_target("deleted-sid") is None


# ── against the real Agent SessionDB when available ─────────────────────────


@pytest.mark.requires_agent_modules
def test_real_session_db_row_gets_workspace(tmp_path):
    hermes_state = pytest.importorskip("hermes_state")
    db = hermes_state.SessionDB(db_path=tmp_path / "state.db")
    try:
        db.create_session(session_id="real", source="webui", model="m", cwd=None)
        assert state_sync.sync_session_cwd("real", "/home/u/workspace/", db=db) is True
        row = db.get_session("real")
        assert row["cwd"] == "/home/u/workspace"
        assert state_sync.sync_session_cwd("real", "/home/u/workspace", db=db) is False
        assert state_sync.sync_session_cwd("missing", "/home/u/workspace", db=db) is False
        assert db.get_session("missing") is None

        db.create_session(session_id="cli-row", source="cli", model="m", cwd="/projects/cli")
        db.update_session_cwd("cli-row", "/projects/cli", git_branch="main", git_repo_root="/projects/cli")
        assert state_sync.sync_session_cwd("cli-row", "/projects/webui", db=db) is False
        cli = db.get_session("cli-row")
        assert (cli["cwd"], cli["git_repo_root"]) == ("/projects/cli", "/projects/cli")
    finally:
        db.close()


@pytest.fixture
def real_homes(tmp_path, monkeypatch):
    """Root and named-profile homes with real ``state.db`` files, the named one
    active process-wide (the setup the profile=None review finding used)."""
    hermes_state = pytest.importorskip("hermes_state")
    import api.profiles as profiles

    root, named = tmp_path / "root", tmp_path / "profiles" / "work"
    root.mkdir()
    named.mkdir(parents=True)
    monkeypatch.setattr(profiles, "get_active_hermes_home", lambda: named)
    monkeypatch.setattr(
        profiles, "_resolve_profile_home_for_name",
        lambda name: root if name == "default" else named,
    )
    monkeypatch.setattr(profiles, "_is_root_profile", lambda name: name == "default")
    dbs = {k: hermes_state.SessionDB(db_path=h / "state.db") for k, h in (("root", root), ("work", named))}
    yield dbs
    for db in dbs.values():
        db.close()


@pytest.mark.requires_agent_modules
def test_real_db_profile_none_updates_the_root_row_not_the_active_profile(real_homes):
    root_db, work_db = real_homes["root"], real_homes["work"]
    root_db.create_session(session_id="legacy", source="webui", model="m", cwd=None)
    work_db.create_session(session_id="legacy", source="webui", model="m", cwd="/home/u/active")
    state_sync.sync_session_cwd_background(lambda: ("legacy", "/home/u/workspace", None))
    assert state_sync.drain_cwd_syncs()
    assert root_db.get_session("legacy")["cwd"] == "/home/u/workspace"
    assert work_db.get_session("legacy")["cwd"] == "/home/u/active"


@pytest.mark.requires_agent_modules
def test_real_db_trailing_space_workspace_is_recorded_verbatim(tmp_path):
    hermes_state = pytest.importorskip("hermes_state")
    plain, spaced = tmp_path / "acme", tmp_path / "acme "
    plain.mkdir()
    spaced.mkdir()
    db = hermes_state.SessionDB(db_path=tmp_path / "state.db")
    try:
        db.create_session(session_id="sp", source="webui", model="m", cwd=str(plain))
        assert state_sync.sync_session_cwd("sp", str(spaced), db=db) is True
        assert db.get_session("sp")["cwd"] == str(spaced)
        assert state_sync.sync_session_cwd("sp", str(spaced), db=db) is False
    finally:
        db.close()


@pytest.mark.requires_agent_modules
def test_real_db_and_real_session_cache_survive_eviction_between_updates(real_homes, tmp_path):
    """Real ``get_session`` / ``SESSIONS`` / on-disk sidecar and a real
    ``SessionDB``: evict the cached object between two workspace changes and run
    the delayed syncs newest-first."""
    import api.routes as routes
    from api.config import SESSIONS

    ws = [tmp_path / n for n in "abc"]
    for d in ws:
        d.mkdir()
    s = Session(session_id="evict-sid", title="t", profile="default")
    s.workspace = str(ws[0])
    s.save()
    SESSIONS.pop("evict-sid", None)
    real_homes["root"].create_session(session_id="evict-sid", source="webui", model="m", cwd=str(ws[0]))

    first = routes.get_session("evict-sid")
    first.workspace = str(ws[1])
    first.save()
    old_target = lambda _s=first: (_s.session_id, _s.workspace, None)  # the pre-fix capture
    SESSIONS.pop("evict-sid", None)  # LRU eviction: next get_session reloads a DISTINCT object
    second = routes.get_session("evict-sid")
    assert second is not first
    second.workspace = str(ws[2])
    second.save()

    assert old_target()[1] == str(ws[1])  # what a captured object would have written
    for _ in range(2):  # both delayed callbacks, newest first, then the older one
        target = routes._current_cwd_sync_target("evict-sid")
        assert target[1] == str(ws[2])
        state_sync.sync_session_cwd(*target)
    assert real_homes["root"].get_session("evict-sid")["cwd"] == str(ws[2])
    SESSIONS.pop("evict-sid", None)
