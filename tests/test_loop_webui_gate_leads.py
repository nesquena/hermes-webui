"""/loop gate leads: tombstoned profile dirs, Stop after Steer, server-settled cancels, admission, judge env."""
from types import SimpleNamespace as NS

import pytest

from tests.conftest import requires_agent_modules
from tests.test_cancel_restart_journal_recovery import (
    _isolated_state,  # noqa: F401 - imported autouse isolation fixture
    _start_cancelled_turn,
)
from tests.test_loop_webui import _due, _loop, _setup, _stop_marker


def test_stop_after_steer_stamps_only_the_original_prompt_row():
    """A mid-turn Steer row must not receive the cancelled prompt's turn token."""
    from api import models
    from api.process_event_utils import build_active_turn_token
    from api.streaming import cancel_stream
    sid, stream = "steer-stop", "steer-stop-run"
    session = _start_cancelled_turn(sid, stream)
    token = build_active_turn_token(stream, session.pending_started_at)
    session.messages = [
        {"role": "user", "content": session.pending_user_message, "timestamp": 10.0, "_active_turn_token": token},
        {"role": "user", "content": "also check logs", "timestamp": 12.0, "display_kind": "steer"},
    ]
    session.save()
    assert cancel_stream(stream)
    rows = [m for m in models.get_session(sid).messages if m.get("_active_turn_token") == token]
    assert [r["content"] for r in rows] == ["Do the cancellable task."]


@requires_agent_modules
def test_tombstone_dir_does_not_clear_a_default_profile_loop(tmp_path, monkeypatch):
    """Maintainer repro: `profiles/.deleted` must not be scanned as a profile (real resolver)."""
    from api import profiles
    root_home = tmp_path / "home"
    (root_home / "profiles" / ".deleted").mkdir(parents=True)
    (root_home / "profiles" / ".deleted" / "gone").write_text("deleted\n")
    (root_home / "profiles" / "gone").mkdir()  # tombstoned leftover dir
    (root_home / "profiles" / "Not_A_Valid_Id").mkdir()
    monkeypatch.setattr(profiles, "_DEFAULT_HERMES_HOME", root_home)
    session = NS(session_id="s1", profile=None, messages=[])
    agent, loops, started = _setup(tmp_path, monkeypatch, session, due=False,
                                   home=profiles._resolve_profile_home_for_name,
                                   root=root_home / "profiles")
    monkeypatch.setattr(profiles, "get_hermes_home_for_profile", profiles._resolve_profile_home_for_name)
    monkeypatch.setattr(profiles, "_profiles_root", lambda: (root_home / "profiles").resolve())
    homes = []
    real_home = loops._home

    def spy(p):
        homes.append(p)
        return real_home(p)

    monkeypatch.setattr(loops, "_home", spy)
    with real_home(None):  # an hour out, as in the maintainer's repro
        s = agent.load_loop("s1")
        s.next_due_at = 4e9
        agent.save_loop("s1", s)
    homes.clear()
    loops.run_due_loops()  # not due: control pass
    scanned = list(homes)
    assert _loop(agent, loops).status == "active" and started == []
    assert scanned == [None]  # tombstone, tombstoned profile and invalid id are never scanned
    with real_home(None):
        _due(agent)
    loops.run_due_loops()
    assert len(started) == 1 and _loop(agent, loops).status == "active"


@requires_agent_modules
def test_profile_name_mismatch_with_same_home_never_clears(tmp_path, monkeypatch):
    """Two names resolving to one home (alias) must not clear: compare resolved homes."""
    session = NS(session_id="s1", profile="default", messages=[])
    agent, loops, started = _setup(tmp_path, monkeypatch, session, due=True,
                                   home=lambda p: tmp_path / "default")
    with loops._home("alias"):
        loops._run_one("alias", "s1")
    with loops._home(None):
        assert agent.load_loop("s1").status == "active"
    assert len(started) == 1


@requires_agent_modules
def test_profile_move_pauses_instead_of_clearing(tmp_path, monkeypatch):
    """A session now owned by another profile: the stale loop is paused, never launched or deleted."""
    session = NS(session_id="s1", profile="b", messages=[])
    agent, loops, started = _setup(tmp_path, monkeypatch, session, cmd="", due=False)
    session.profile = None
    loops.run_loop_command("s1", "5m check")
    with loops._home(None):
        _due(agent)
    session.profile = "b"
    loops.run_due_loops()
    s = _loop(agent, loops)
    assert started == [] and s.status == "paused" and "another profile" in s.paused_reason


@requires_agent_modules
def test_server_settled_cancel_is_not_judged_as_a_user_stop(tmp_path, monkeypatch):
    session = NS(session_id="s1", profile=None, messages=[])
    agent, loops, started = _setup(tmp_path, monkeypatch, session)
    loops.run_due_loops()
    session.messages.append(_server_settle_row())
    loops.run_due_loops()
    s = _loop(agent, loops)
    assert s.status == "active" and not s.awaiting_response and s.ticks_fired == 0  # rolled back, fires again
    with loops._home(None):
        _due(agent)
    loops.run_due_loops()
    assert len(started) == 2


@requires_agent_modules
@pytest.mark.parametrize("attr,value", [("read_only", True), ("_sidecar_names_other_session", "x")])
def test_loop_command_refuses_a_chat_that_cannot_be_written(tmp_path, monkeypatch, attr, value):
    session = NS(session_id="s1", profile=None, messages=[])
    agent, loops, started = _setup(tmp_path, monkeypatch, session, cmd="", due=False)
    setattr(session, attr, value)
    out = loops.run_loop_command("s1", "5m check")
    assert "can't" in out and _loop(agent, loops) is None
    assert "No active loop" in loops.run_loop_command("s1", "stop")  # controls still work


@requires_agent_modules
def test_until_judge_runs_in_the_session_profile_env(tmp_path, monkeypatch):
    from api import profiles
    from hermes_cli import goals
    from contextlib import contextmanager
    session = NS(session_id="s1", profile="alice", messages=[])
    (tmp_path / "profiles" / "alice").mkdir(parents=True)
    agent, loops, started = _setup(tmp_path, monkeypatch, session, cmd="", due=False, root=tmp_path / "profiles")
    scope = []

    @contextmanager
    def env(sess, purpose="", *a, **kw):
        scope.append(sess if isinstance(sess, str) else getattr(sess, "profile", sess))
        yield
        scope.pop()

    seen = []
    monkeypatch.setattr(profiles, "profile_env_for_background_worker", env)
    monkeypatch.setattr(goals, "judge_goal", lambda *a, **kw: (seen.append(list(scope)), ("continue", "no", False, None, False))[1])
    loops.run_loop_command("s1", "5m watch --until queue is empty")
    with loops._home("alice"):
        _due(agent)
    loops.run_due_loops()
    session.messages.append({"role": "assistant", "content": "still busy"})
    loops.run_due_loops()
    assert seen == [["alice"]]


@requires_agent_modules
@pytest.mark.parametrize("record", [None, {"tick": 1}])
def test_tick_fired_but_never_launched_is_rolled_back_not_settled(tmp_path, monkeypatch, record):
    """A crash between fire_tick and the launch record must not count a tick that never ran."""
    import json
    session = NS(session_id="s1", profile=None, messages=[{"role": "assistant", "content": "old\nLOOP_COMPLETE"}])
    agent, loops, started = _setup(tmp_path, monkeypatch, session)
    with loops._home(None):
        mgr = agent.LoopManager(session_id="s1")
        assert mgr.fire_tick()  # the process dies here, before start_session_turn returns
        db = agent._get_session_db()
        if record:
            loops._meta_set(db, loops._TURN_PREFIX + "s1", json.dumps(record))
    loops.run_due_loops()
    s = _loop(agent, loops)
    assert s.status == "active" and s.ticks_fired == 0 and not s.awaiting_response and started == []


def test_busy_alias_keeps_a_draft_typed_during_the_metadata_wait():
    """Maintainer repro: busy `/proactive pause`, then a new draft before metadata resolves."""
    from tests.test_loop_webui_busy_owner import _run_send
    out = _run_send("""
  ctx.__msg.value='/proactive pause'; const first=ctx.send(); await flush();
  ctx.__msg.value='new draft'; release(); await first; out.composer=ctx.__msg.value;""", "true", strict=True)
    assert out["posts"] == [["/api/commands/exec", {"command": "/loop pause", "session_id": "A"}]]
    assert out["composer"] == "new draft"


def _server_settle_row():
    return _stop_marker(message="Server restarting.", initiator="server")


def _restart_interrupted_row():
    from api.models import _interrupted_recovery_marker
    return _interrupted_recovery_marker(recovered_output=False)


@requires_agent_modules
@pytest.mark.parametrize("make_row,status,ticks,reason", [
    (lambda: {"role": "assistant", "content": 'The log says "response interrupted"; it reconnected.'},
     "active", 1, None),  # ordinary prose quoting the diagnostic: a completed tick, as in the CLI
    (lambda: {"role": "assistant", "content": "Task cancelled. was the last log line"}, "active", 1, None),
    (_stop_marker, "paused", 1, "user-interrupted (Stop)"),
    (_server_settle_row, "active", 0, None),  # server settle: rolled back, fires again
    (_restart_interrupted_row, "active", 0, None),  # process restart lost the turn: not the user's Stop
])
def test_wakeup_settles_on_cancel_provenance_not_reply_text(tmp_path, monkeypatch, make_row, status, ticks, reason):
    session = NS(session_id="s1", profile=None, messages=[])
    agent, loops, started = _setup(tmp_path, monkeypatch, session)
    loops.run_due_loops()
    row = make_row()
    session.messages.append(row)
    loops.run_due_loops()
    s = _loop(agent, loops)
    assert (s.status, s.ticks_fired, s.paused_reason, s.awaiting_response) == (status, ticks, reason, False)
    if status == "active" and ticks:  # same verdict as the native LoopManager for the same reply
        with loops._home(None):
            native = agent.LoopManager(session_id="native")
            native.set("5m check")
            native.fire_tick()
            native.complete_tick(row["content"])
            assert native.state.status == "active"


@requires_agent_modules
def test_gateway_canonical_completion_keeps_the_wakeup_identity(tmp_path, monkeypatch):
    """LOOP_COMPLETE settled through the Gateway's display-history replacement ends the loop."""
    from api.gateway_sessions import display_history
    session = NS(session_id="s1", profile=None, messages=[])
    agent, loops, started = _setup(tmp_path, monkeypatch, session)
    loops.run_due_loops()
    wake = session.messages[-1]
    snapshot = {"display_messages": [  # the Gateway's rows carry its row ids and clock, not WebUI's token
        {"role": "user", "text": wake["content"], "row_id": 7, "timestamp": 1700000000.5},
        {"role": "assistant", "text": "all green\nLOOP_COMPLETE", "row_id": 8}]}
    session.messages = display_history(snapshot, wake["_active_turn_token"], wake["content"])
    loops.run_due_loops()
    with loops._home(None):
        _due(agent)
    loops.run_due_loops()
    assert _loop(agent, loops).status == "done" and len(started) == 1


def _provider_error_row(tmp_path, monkeypatch, mode):
    """The row the real worker persists when the provider call is interrupted (raised or returned failed)."""
    import queue
    from api import config, models, profiles, routes, streaming

    class Agent:
        def __init__(self, **kw):
            self.session_id, self.platform, self.model = "perr", "webui", "m"
            self.context_compressor = self.ephemeral_system_prompt = None

        def run_conversation(self, user_message, conversation_history=None, **kw):
            if mode == "raise":
                raise RuntimeError("Operation was interrupted")
            return {"completed": False, "failed": True, "error": "Request was aborted", "final_response": None,
                    "messages": list(conversation_history or []) + [{"role": "user", "content": user_message}]}

    with monkeypatch.context() as m:
        m.setattr(streaming, "_get_ai_agent", lambda: Agent)
        m.setattr(streaming, "_build_session_db_for_stream", lambda _: None)
        m.setattr(streaming, "resolve_model_provider", lambda *a, **k: ("m", None, None))
        m.setattr(streaming, "get_config", lambda: {})
        m.setattr(config, "get_config", lambda: {})
        m.setattr(config, "_resolve_cli_toolsets", lambda *a, **k: [])
        m.setattr(profiles, "get_active_hermes_home", lambda: tmp_path)
        config.SESSION_AGENT_CACHE.clear()  # a cached agent from the other mode would answer instead
        s = models.Session(session_id="perr", model="m")
        s.save()
        models.SESSIONS["perr"] = s
        routes._prepare_chat_start_session_for_stream(s, msg="Q", attachments=[], workspace=str(tmp_path), model="m",
                                                      model_provider=None, stream_id="perr-run", started_at=5.0)
        config.STREAMS["perr-run"] = queue.Queue()
        streaming._run_agent_streaming("perr", "Q", "m", str(tmp_path), "perr-run", [])
        row = models.Session.load("perr").messages[-1]
    assert row["_error"] is True and row["provider_details_label"] == "Interruption details" and "type" not in row
    assert row["provider_details"] == ("Operation was interrupted" if mode == "raise" else "Request was aborted")
    return row


@requires_agent_modules
@pytest.mark.parametrize("mode", ["raise", "result"])
@pytest.mark.parametrize("cmd,times,status", [("5m check", 0, "active"), ("5m --times 1 check", 1, "done")])
def test_provider_interrupted_wakeup_is_a_failed_tick_like_the_cli(tmp_path, monkeypatch, mode, cmd, times, status):
    """A provider-interrupted wakeup counts as a tick judged on "" (the CLI's failed-turn verdict), never its error text."""
    row = _provider_error_row(tmp_path, monkeypatch, mode)
    session = NS(session_id="s1", profile=None, messages=[])
    agent, loops, started = _setup(tmp_path, monkeypatch, session, cmd=cmd)
    judged = []
    real = agent.LoopManager.complete_tick
    monkeypatch.setattr(agent.LoopManager, "complete_tick", lambda self, reply: judged.append(reply) or real(self, reply))
    loops.run_due_loops()
    session.messages.append(row)
    loops.run_due_loops()
    s = _loop(agent, loops)
    assert (s.status, s.ticks_fired, s.paused_reason, s.awaiting_response, judged) == (status, 1, None, False, [""])
    with loops._home(None):  # the native manager reaches the same state for a failed turn
        native = agent.LoopManager(session_id="native")
        native.set("check", interval_seconds=300, times=times)
        native.fire_tick()
        assert native.complete_tick("")["status"] == status


def _seal(monkeypatch, tip="c1"):
    """Durable lineage says s1 was compressed into `tip` (by CLI/Desktop); the WebUI sidecar is untouched."""
    from api import compression_continuation
    monkeypatch.setattr(compression_continuation, "durable_compression_continuation",
                        lambda session: (True, tip) if session.session_id == "s1" else (False, None))


@requires_agent_modules
def test_loop_on_an_externally_compressed_chat_is_refused_like_chat_start(tmp_path, monkeypatch):
    """The stale sidecar looks writable; /loop must 409 session_rotated with no loop state and no worker."""
    from urllib.parse import urlparse
    from api import routes
    session = NS(session_id="s1", profile=None, messages=[])
    agent, loops, started = _setup(tmp_path, monkeypatch, session, cmd="", due=False)
    _seal(monkeypatch)
    assert loops._chat_writable(session)
    out = {}
    monkeypatch.setattr(routes, "_check_csrf", lambda h: True)
    monkeypatch.setattr(routes, "_get_active_profile_name", lambda: None)
    monkeypatch.setattr(routes, "read_body", lambda h: {"command": "/loop 5m check", "session_id": "s1"})
    monkeypatch.setattr(routes, "j", lambda h, payload, status=200, **kw: out.update(payload, _status=status))
    routes.handle_post(NS(headers={}), urlparse("/api/commands/exec"))
    assert out == {"error": "This session was compressed. Open its continuation before starting a loop.",
                   "code": "session_rotated", "continuation_session_id": "c1", "_status": 409}
    assert _loop(agent, loops) is None and started == []
    assert loops.run_loop_command("s1", "status")  # read-only controls still answer on the old id


@requires_agent_modules
@pytest.mark.parametrize("child_sidecar,owner,status", [(True, "c1", "active"), (False, "s1", "paused")])
def test_registered_loop_never_fires_on_a_sealed_parent(tmp_path, monkeypatch, child_sidecar, owner, status):
    """Compressed after /loop: the loop follows the WebUI continuation (Agent migration), else pauses; never fires on s1."""
    from api import config
    session = NS(session_id="s1", profile=None, messages=[])
    agent, loops, started = _setup(tmp_path, monkeypatch, session)
    monkeypatch.setattr(config, "SESSION_DIR", tmp_path / "sidecars")
    (tmp_path / "sidecars").mkdir()
    if child_sidecar:
        (tmp_path / "sidecars" / "c1.json").write_text("{}")
    _seal(monkeypatch)
    loops.run_due_loops()
    assert started == []
    s = _loop(agent, loops, sid=owner)
    assert (s.status, s.ticks_fired) == (status, 0)
    if child_sidecar:
        assert _loop(agent, loops).status == "cleared"
    else:
        assert s.paused_reason == "session compressed"


@requires_agent_modules
def test_replacement_loop_racing_compression_carry_is_rejected_and_retired(tmp_path, monkeypatch):
    """Maintainer schedule: /loop NEW passes the unsealed check, compression publishes and the native carry
    reads OLD, NEW saves, then the carry writes OLD on the child and clears the parent. The command must
    409 session_rotated and neither generation may stay runnable on the continuation."""
    import threading
    from urllib.parse import urlparse
    from api import compression_continuation, config, routes
    session = NS(session_id="s1", profile=None, messages=[])
    agent, loops, started = _setup(tmp_path, monkeypatch, session, cmd="5m OLD INTENT", due=False)
    monkeypatch.setattr(config, "SESSION_DIR", tmp_path / "sidecars")
    (tmp_path / "sidecars").mkdir()
    (tmp_path / "sidecars" / "c1.json").write_text("{}")
    published = threading.Event()
    monkeypatch.setattr(compression_continuation, "durable_compression_continuation",
                        lambda s: (True, "c1") if s.session_id == "s1" and published.is_set() else (False, None))
    real_dispatch, real_save = agent.dispatch_loop_command, agent.save_loop
    cmd_parked, cmd_go, carry_parked, carry_go = (threading.Event() for _ in range(4))

    def dispatch(*a, **kw):  # barrier 1: the replacement passed the unsealed check
        cmd_parked.set()
        assert cmd_go.wait(5)
        return real_dispatch(*a, **kw)

    def save(sid, state):  # barrier 2: the carry read the parent's OLD row, before writing the child
        if sid == "c1":
            carry_parked.set()
            assert carry_go.wait(5)
        return real_save(sid, state)

    monkeypatch.setattr(agent, "dispatch_loop_command", dispatch)
    monkeypatch.setattr(agent, "save_loop", save)
    out = {}
    monkeypatch.setattr(routes, "_check_csrf", lambda h: True)
    monkeypatch.setattr(routes, "_get_active_profile_name", lambda: None)
    monkeypatch.setattr(routes, "read_body", lambda h: {"command": "/loop 5m NEW INTENT", "session_id": "s1"})
    monkeypatch.setattr(routes, "j", lambda h, payload, status=200, **kw: out.update(payload, _status=status))

    def carry():
        with loops._home(None):
            agent.migrate_loop_to_session("s1", "c1", reason="compression")

    cmd = threading.Thread(target=routes.handle_post, args=(NS(headers={}), urlparse("/api/commands/exec")))
    cmd.start()
    assert cmd_parked.wait(5)
    published.set()  # compression publishes the continuation...
    mig = threading.Thread(target=carry)
    mig.start()
    assert carry_parked.wait(5)  # ...and the carry holds OLD
    cmd_go.set()
    cmd.join(5)
    carry_go.set()
    mig.join(5)
    assert out.get("code") == "session_rotated" and out["_status"] == 409, out
    with loops._home(None):
        _due(agent, "c1")
    session.session_id = "c1"
    loops.run_due_loops()
    assert started == []
    assert _loop(agent, loops, "c1").status == "cleared" and _loop(agent, loops).status == "cleared"


def _gateway_turn(monkeypatch, tmp_path, s, stream_id, prompt, started_at, display):
    """Settle one turn through the real native-Gateway path; `display` is the Gateway's whole transcript."""
    import asyncio
    import sys
    import threading
    from contextlib import asynccontextmanager
    from types import ModuleType
    from api import gateway_chat, gateway_sessions
    from api.config import STREAMS, STREAMS_LOCK, create_stream_channel
    protocol = ModuleType("hermes_cli.gateway_client")
    protocol.GatewayRPCError = type("GatewayRPCError", (Exception,), {})
    monkeypatch.setitem(sys.modules, "hermes_cli.gateway_client", protocol)
    monkeypatch.setattr(gateway_sessions, "config_for_session", lambda _: {"webui_gateway_session_transport": "local"})
    monkeypatch.setattr(gateway_chat, "_gateway_reasoning_effort_for_request", lambda *a, **k: None)

    class Client:
        events = asyncio.Queue()

        async def rpc(self, method, **params):
            if method == "session.resume":
                return {"session_id": s.session_id, "display_messages": display}
            return {"admission_id": stream_id, "status": "terminal", "outcome": "completed",
                    "result": {"final_response": display[-1]["text"]}}

    @asynccontextmanager
    async def connect(home):
        yield Client()

    monkeypatch.setattr(gateway_sessions, "connect_local", connect)
    s.active_stream_id, s.pending_user_message, s.pending_started_at = stream_id, prompt, started_at
    s.pending_attachments, s.pending_user_source = [], "webui"
    s.save()
    with STREAMS_LOCK:
        STREAMS[stream_id] = create_stream_channel()
    worker = threading.Thread(target=gateway_chat._run_gateway_chat_streaming,
                              args=(s.session_id, prompt, s.model, str(tmp_path), stream_id, []))
    worker.start()
    worker.join(20)
    with STREAMS_LOCK:
        STREAMS.pop(stream_id, None)
    assert not worker.is_alive() and s.active_stream_id is None


@requires_agent_modules
@pytest.mark.parametrize("cmd,reply,status", [
    ("5m check", "all green\nLOOP_COMPLETE", "done"),
    ("5m check --until the deploy is live", "the deploy is live", "done"),
])
def test_later_gateway_turn_keeps_the_wakeup_identity(tmp_path, monkeypatch, cmd, reply, status):
    """Another Gateway turn settling before the scheduler judges the wakeup must not hide its outcome."""
    from collections import OrderedDict
    from hermes_cli import goals
    from api import models
    sessions = tmp_path / "gw-sessions"
    sessions.mkdir()
    monkeypatch.setattr(models, "SESSION_DIR", sessions)
    monkeypatch.setattr(models, "SESSION_INDEX_FILE", sessions / "_index.json")
    monkeypatch.setattr(models, "SESSIONS", OrderedDict())
    monkeypatch.setenv("HERMES_WEBUI_CHAT_BACKEND", "gateway")
    s = models.new_session()
    s.model = "m"
    judged = []
    monkeypatch.setattr(goals, "judge_goal", lambda cond, text, *a, **k: (judged.append(text), (
        "done" if text == "the deploy is live" else "continue", "met", False, None, False))[1])
    agent, loops, started = _setup(tmp_path, monkeypatch, s, cmd="", due=False,
                                   get_session=lambda sid, **kw: s)
    assert "Loop" in loops.run_loop_command(s.session_id, cmd), "loop not started"
    with loops._home(None):
        _due(agent, s.session_id)
    loops.run_due_loops()
    s.source_tag = "desktop"  # route the settlements through the native Gateway session transport
    wake = s.messages[-1]
    s.messages.pop()  # the Gateway path re-materializes the pending prompt itself
    wake_rows = [{"role": "user", "text": wake["content"], "row_id": 7, "timestamp": 1700000000.5},
                 {"role": "assistant", "text": reply, "row_id": 8, "timestamp": 1700000001.0}]
    _gateway_turn(monkeypatch, tmp_path, s, "stream1", wake["content"], wake["timestamp"], wake_rows)
    _gateway_turn(monkeypatch, tmp_path, s, "stream-user", "and another thing", 2000.0, wake_rows + [
        {"role": "user", "text": "and another thing", "row_id": 9, "timestamp": 1700000002.5},
        {"role": "assistant", "text": "sure", "row_id": 10, "timestamp": 1700000003.0}])
    loops.run_due_loops()
    with loops._home(None):
        _due(agent, s.session_id)
    loops.run_due_loops()
    assert _loop(agent, loops, s.session_id).status == status and len(started) == 1
    assert judged == ([reply] if "--until" in cmd else [])
