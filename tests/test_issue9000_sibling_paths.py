"""#9000 sibling paths: a session reloaded from a copy of another session's file never writes
to, or deletes, that other session.

When a session's file is replaced with a copy of another session's file, the #8852 loader marks
the reloaded object (``_sidecar_names_other_session``). The object carries the other session's
``session_id``, so its ``path`` is the other session's file. Two writers used it unchecked:

- ``_current_cwd_sync_target`` (``api/routes.py``), the resolver of the delayed state.db cwd
  mirror behind ``POST /api/session/update`` and the sync ``POST /api/chat``, returned the other
  session's id, workspace and profile, so the mirror rewrote that session's state.db row;
- the ``/btw`` success path (``api/streaming.py``) unlinks ``s.path``, which for a marked
  snapshot is the other conversation. Today the worker's pre-run save refuses a marked snapshot
  (#8852), so the turn fails before that cleanup; the cleanup now checks the mark itself rather
  than relying on that save.

Both now skip a marked object. Unmarked sessions behave as before (the positive controls).
"""

import queue
import sys
import types
from unittest import mock
from urllib.parse import urlparse

import pytest

from tests.test_issue8852_body_id_write_target import VICTIM, _victim, store  # noqa: F401 (fixture)

OWN = "own-9000"


def _own(directory, workspace, *, active_stream_id=None):
    from api import models

    session = models.Session(session_id=OWN, title="Own", profile="default")
    session.workspace = str(workspace)
    session.active_stream_id = active_stream_id
    session.save()
    models.SESSIONS.clear()
    return directory / f"{OWN}.json"


def _swap(directory):
    """Replace the own file with a copy of the victim's and drop the cache (an LRU eviction)."""
    from api import models

    (directory / f"{OWN}.json").write_bytes((directory / f"{VICTIM}.json").read_bytes())
    models.SESSIONS.clear()


# --- the cwd mirror -----------------------------------------------------------------------


def test_cwd_sync_target_skips_a_session_swapped_for_another(store, tmp_path):  # noqa: F811 -- pytest fixture parameter
    import api.routes as routes

    _victim(store, workspace=tmp_path)
    _own(store, tmp_path / "own")
    assert routes._current_cwd_sync_target(OWN) == (OWN, str(tmp_path / "own"), "default")

    _swap(store)
    assert routes._current_cwd_sync_target(OWN) is None


@pytest.mark.parametrize("swap", [False, True])
def test_workspace_update_never_mirrors_into_the_swapped_in_session(store, tmp_path, monkeypatch, swap):  # noqa: F811 -- pytest fixture parameter
    """``POST /api/session/update`` with real per-profile state.db files. The own file is swapped
    after the route saved and before the delayed mirror resolves. On master the mirror wrote the
    victim's old workspace (from the copy) into the victim's row in the victim's profile."""
    hermes_state = pytest.importorskip("hermes_state")
    import api.routes as routes
    import api.state_sync as state_sync

    dbs = {p: tmp_path / f"state-{p}.db" for p in ("default", "work")}
    monkeypatch.setattr(state_sync, "_get_state_db", lambda profile=None: hermes_state.SessionDB(db_path=dbs[profile]))

    def _db_call(profile, method, *args, **kwargs):
        db = hermes_state.SessionDB(db_path=dbs[profile])
        try:
            return getattr(db, method)(*args, **kwargs)
        finally:
            db.close()

    old_ws, new_ws, victim_old, victim_now = (tmp_path / n for n in ("own-a", "own-b", "victim-a", "victim-b"))
    for d in (old_ws, new_ws, victim_old, victim_now):
        d.mkdir()
    _victim(store, workspace=victim_old)  # the copy carries this stale workspace
    _db_call("work", "create_session", VICTIM, "webui", cwd=str(victim_now))
    _own(store, old_ws)
    _db_call("default", "create_session", OWN, "webui", cwd=str(old_ws))

    schedule = state_sync.sync_session_cwd_background

    def _swap_then_schedule(resolve):
        if swap:
            _swap(store)
        return schedule(resolve)

    replies = []
    monkeypatch.setattr(state_sync, "sync_session_cwd_background", _swap_then_schedule)
    monkeypatch.setattr(routes, "_check_csrf", lambda _h: True, raising=False)
    monkeypatch.setattr(routes, "read_body", lambda _h: {"session_id": OWN, "workspace": str(new_ws)})
    monkeypatch.setattr(routes, "resolve_trusted_workspace", lambda ws, **_k: ws)
    monkeypatch.setattr(routes, "set_last_workspace", lambda *a, **k: None)
    monkeypatch.setattr(routes, "j", lambda _h, obj, *a, **k: replies.append(obj) or True)
    monkeypatch.setattr(routes, "bad", lambda _h, msg, code=400: pytest.fail(f"{code} {msg}"))

    class _Handler:
        headers = {"Content-Type": "application/json"}
        command = "POST"
        path = "/api/session/update"
        client_address = ("127.0.0.1", 0)

    routes.handle_post(_Handler(), urlparse("/api/session/update"))
    assert state_sync.drain_cwd_syncs()

    assert replies and replies[0]["session"]["workspace"] == str(new_ws)
    assert _db_call("work", "get_session", VICTIM)["cwd"] == str(victim_now), "the victim's row was rewritten"
    assert _db_call("default", "get_session", OWN)["cwd"] == str(old_ws if swap else new_ws)


# --- the /btw unlink ----------------------------------------------------------------------


def _btw_turn(directory, monkeypatch, *, swap, marked_saves_pass=False):
    """Drive the real worker's ``/btw`` success path; the Agent double only answers.

    ``marked_saves_pass`` lets the worker's saves of a marked snapshot through as no-ops (a worker
    that no longer saves ``/btw`` turns), so the turn reaches its cleanup."""
    import api.config as config
    import api.streaming as streaming
    from api import models

    stream_id = "stream-9000-btw"
    victim = _victim(directory).read_bytes()
    _own(directory, directory, active_stream_id=stream_id)
    if swap:  # replaced before the worker loads its snapshot
        _swap(directory)
    runtime = types.ModuleType("hermes_cli.runtime_provider")
    runtime.resolve_runtime_provider = lambda requested=None, **_kw: {
        "provider": requested or "test-provider", "api_key": "synthetic-key", "base_url": None}
    cli = types.ModuleType("hermes_cli")
    cli.runtime_provider = runtime
    state = types.ModuleType("hermes_state")
    state.SessionDB = mock.Mock(return_value=None)
    for name, module in (("hermes_cli", cli), ("hermes_cli.runtime_provider", runtime), ("hermes_state", state)):
        monkeypatch.setitem(sys.modules, name, module)
    monkeypatch.setattr(streaming, "_attempt_credential_self_heal", lambda *a, **k: None)
    if marked_saves_pass:
        # #9019: get_session no longer returns a marked snapshot; let the worker see it so the
        # cleanup's own guard is still exercised.
        monkeypatch.setattr(streaming, "get_session", lambda sid, *a, **k: models.get_session(
            sid, *a, allow_foreign_body=True, **k))
        save = models.Session.save
        monkeypatch.setattr(models.Session, "save", lambda self, *a, **k: (
            None if getattr(self, "_sidecar_names_other_session", None) else save(self, *a, **k)))

    class _Agent:
        def __init__(self, **kwargs):
            self.session_id = kwargs.get("session_id")
            self._session_db = None
            self.stream_delta_callback = kwargs.get("stream_delta_callback")
            self.session_prompt_tokens = self.session_completion_tokens = 0
            self.session_estimated_cost_usd = 0.0
            self.context_compressor = self._last_error = self.ephemeral_system_prompt = None

        def run_conversation(self, **kwargs):
            history = list(kwargs.get("conversation_history") or [])
            return {"status": "ok", "messages": history + [{"role": "assistant", "content": "side answer"}]}

        def interrupt(self, _message):
            pass

    q = queue.Queue()
    config.STREAMS[stream_id] = q
    try:
        with mock.patch.object(streaming, "_get_ai_agent", return_value=_Agent), \
             mock.patch.object(streaming, "resolve_model_provider", return_value=("m", "test-provider", None)), \
             mock.patch("api.config.get_config", return_value={}), \
             mock.patch("api.config._resolve_cli_toolsets", return_value=[]):
            streaming._run_agent_streaming(OWN, "side q", "m", str(directory), stream_id, ephemeral=True)
    finally:
        for registry in (config.STREAMS, config.CANCEL_FLAGS, config.STREAM_PARTIAL_TEXT):
            registry.pop(stream_id, None)
    return [event for event, _payload in q.queue], victim


def test_btw_turn_on_a_swapped_snapshot_fails_before_its_cleanup(store, monkeypatch):  # noqa: F811 -- pytest fixture parameter
    """Pin (passes on master too): the pre-run save refuses the marked snapshot, so the turn ends in
    an ``apperror`` and the victim's file is untouched."""
    events, before = _btw_turn(store, monkeypatch, swap=True)

    assert "apperror" in events and "done" not in events, events
    assert (store / f"{VICTIM}.json").read_bytes() == before


def test_btw_answer_never_deletes_the_swapped_in_session(store, monkeypatch):  # noqa: F811 -- pytest fixture parameter
    """Without the pre-run save refusing first, the turn answers and its cleanup must not unlink
    the marked snapshot's ``path``, the victim's file."""
    events, before = _btw_turn(store, monkeypatch, swap=True, marked_saves_pass=True)

    assert "done" in events, events
    victim_path = store / f"{VICTIM}.json"
    assert victim_path.exists(), "the /btw cleanup deleted another conversation"
    assert victim_path.read_bytes() == before


def test_btw_answer_still_removes_its_own_session_file(store, monkeypatch):  # noqa: F811 -- pytest fixture parameter
    events, _victim_bytes = _btw_turn(store, monkeypatch, swap=False)

    assert "done" in events, events
    assert not (store / f"{OWN}.json").exists()
    assert (store / f"{VICTIM}.json").exists()


# --- the sync chat route ---------------------------------------------------------------------


@pytest.mark.parametrize("swap", [False, True])
def test_sync_chat_refuses_a_session_swapped_for_another(store, tmp_path, monkeypatch, swap):  # noqa: F811 -- pytest fixture parameter
    """``POST /api/chat`` (the sync route) loads the session by id and runs the whole turn under
    the loaded object's ``session_id``. For a marked object that is the other session, so the
    route must refuse before building the Agent, as ``/api/chat/start`` does."""
    import api.routes as routes

    victim_before = _victim(store, workspace=tmp_path).read_bytes()
    _own(store, tmp_path / "own")
    if swap:
        _swap(store)
    runtime = types.ModuleType("hermes_cli.runtime_provider")
    runtime.resolve_runtime_provider = lambda requested=None, **_kw: {
        "provider": requested or "test-provider", "api_key": "synthetic-key", "base_url": None}
    cli = types.ModuleType("hermes_cli")
    cli.runtime_provider = runtime
    for name, module in (("hermes_cli", cli), ("hermes_cli.runtime_provider", runtime)):
        monkeypatch.setitem(sys.modules, name, module)
    built = []

    class _Agent:
        def __init__(self, **kwargs):
            built.append(kwargs.get("session_id"))
            self.session_id = kwargs.get("session_id")
            self._session_db = None
            self.session_prompt_tokens = self.session_completion_tokens = 0
            self.session_estimated_cost_usd = 0.0
            self.context_compressor = self._last_error = self.ephemeral_system_prompt = None

        def run_conversation(self, **kwargs):
            history = list(kwargs.get("conversation_history") or [])
            return {"status": "ok", "messages": history + [{"role": "assistant", "content": "answer"}]}

    replies = []
    monkeypatch.setattr(routes, "require_ai_agent_class", lambda: _Agent)
    monkeypatch.setattr(routes, "resolve_trusted_workspace", lambda ws, **_k: ws)
    monkeypatch.setattr(routes, "j", lambda _h, obj, *a, **k: replies.append(("j", k.get("status", 200), obj)) or True)
    monkeypatch.setattr(routes, "bad", lambda _h, msg, code=400: replies.append(("bad", code, msg)) or True)
    # This test is about the #9000 swapped-session guard. The gateway-backend gate in front of it
    # fails CLOSED when a sidecar's ownership is unverifiable (a swapped file is exactly that, #9024:
    # tests/test_gateway_mode_in_process_refusal.py covers it), so pin the gate to a local rule to
    # reach the guard under test.
    monkeypatch.setattr(routes, "_session_chat_backend_rule", lambda _sid: ({}, None))

    class _Handler:
        headers = {"Content-Type": "application/json"}
        command = "POST"
        path = "/api/chat"
        client_address = ("127.0.0.1", 0)

    with mock.patch.object(routes, "resolve_model_provider", return_value=("m", "test-provider", None), create=True), \
         mock.patch("api.config.resolve_model_provider", return_value=("m", "test-provider", None)), \
         mock.patch("api.config.get_config", return_value={}), \
         mock.patch("api.config._resolve_cli_toolsets", return_value=[]):
        routes._handle_chat_sync(_Handler(), {"session_id": OWN, "message": "hi"})

    assert (store / f"{VICTIM}.json").read_bytes() == victim_before
    if swap:
        assert built == [], "the sync route built an Agent under the other session's id"
        assert replies == [("bad", 404, "Session not found")], replies
    else:
        assert built == [OWN]
        assert replies and replies[0][0] == "j" and replies[0][1] == 200, replies
