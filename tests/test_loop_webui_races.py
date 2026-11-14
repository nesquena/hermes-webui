import threading
from types import SimpleNamespace as NS

import pytest

from tests.conftest import requires_agent_modules
from tests import test_issue7193_eager_start_rollback as _r7193
from tests.test_loop_webui import _due, _loop, _setup, _src

_start7193 = _r7193._start
issue7193_env = _r7193.issue7193_env  # reused fixture: the eager chat-start sandbox


@requires_agent_modules
@pytest.mark.parametrize("pinned,expected_profile,result,loop_status", [
    (False, "b", "retagged", "cleared"),
    (True, None, "pinned_empty", "active"),
])
def test_retag_session_profile_clears_old_loop_only_on_a_move(
        tmp_path, monkeypatch, pinned, expected_profile, result, loop_status):
    session = NS(session_id="s1", profile=None, messages=[], pinned=pinned)
    agent, loops, _ = _setup(tmp_path, monkeypatch, session, due=False)
    from api.routes import _retag_empty_session_profile
    assert loops.retag_session_profile(session, "b", _retag_empty_session_profile) == (expected_profile, result)
    assert session.profile == expected_profile and _loop(agent, loops).status == loop_status
    if result == "retagged":  # both retag sites go through the helper, carrying the request profile
        src = _src("api/routes.py")
        assert src.count("retag_session_profile(\n") == 2
        assert src.count("s, requested_profile, _retag_empty_session_profile") == 2
        assert "request_profile=_get_active_profile_name())" in src


@requires_agent_modules
@pytest.mark.parametrize("mid_pass_writer", ["retag", "cli_stop"])
def test_pass_launches_nothing_after_a_concurrent_move_or_stop(tmp_path, monkeypatch, mid_pass_writer):
    """A retag or a CLI stop landing after the pass loaded the loop: no turn starts, the loop is dropped."""
    session = NS(session_id="s1", profile=None, messages=[])
    agent, loops, started = _setup(tmp_path, monkeypatch, session)

    def mid_pass(sid):
        if mid_pass_writer == "retag":  # chat start retags the empty session after the scheduler listed it
            session.profile = "b"
        else:  # another process (CLI) stops the loop after the scheduler loaded it
            agent.clear_loop(sid)
        return False

    monkeypatch.setattr(agent, "goal_blocks_loop_tick", mid_pass)
    loops.run_due_loops()
    assert started == [] and _loop(agent, loops).status == "cleared"  # the loop left in profile a is dropped


@requires_agent_modules
def test_stop_command_waits_for_scheduler_pass(tmp_path, monkeypatch):
    session = NS(session_id="s1", profile=None, messages=[])
    agent, loops, started = _setup(tmp_path, monkeypatch, session)
    out, t = [], []

    def race(sid):  # /loop stop arrives from a request thread mid-pass
        t.append(threading.Thread(target=lambda: out.append(loops.run_loop_command("s1", "stop"))))
        t[0].start()
        t[0].join(0.3)
        assert t[0].is_alive(), "stop must wait for the scheduler's transition"
        return False

    monkeypatch.setattr(agent, "goal_blocks_loop_tick", race)
    loops.run_due_loops()
    t[0].join(10)
    assert out == ["✓ Loop stopped."] and _loop(agent, loops).status == "cleared"
    loops.run_due_loops()
    assert len(started) == 1  # never brought back after stop reported success


@requires_agent_modules
def test_user_turn_before_judgment_does_not_decide_tick(tmp_path, monkeypatch):
    session = NS(session_id="s1", profile=None, messages=[])
    agent, loops, started = _setup(tmp_path, monkeypatch, session)
    loops.run_due_loops()
    session.messages += [{"role": "assistant", "content": "still deploying"},
                         {"role": "user", "content": "unrelated", "timestamp": 2000.0},
                         {"role": "assistant", "content": "sure\nLOOP_COMPLETE"}]
    loops.run_due_loops()
    s = _loop(agent, loops)
    assert s.status == "active" and not s.awaiting_response and s.ticks_fired == 1


def test_retag_without_agent_modules_still_retags(monkeypatch):
    import sys
    from api import loops
    monkeypatch.setitem(sys.modules, "hermes_cli", None)  # no agent installed: the loop import fails
    s = NS(session_id="s1", profile=None)
    loops.retag_session_profile(s, "b", lambda sess, p: (setattr(sess, "profile", p), (p, "retagged"))[1])
    assert s.profile == "b"


@requires_agent_modules
def test_loop_command_rechecks_profile_under_retag_lock(tmp_path, monkeypatch):
    session = NS(session_id="s1", profile=None, messages=[])
    agent, loops, started = _setup(tmp_path, monkeypatch, session, due=False)
    loops.run_loop_command("s1", "stop")
    # the retag lands after the route's ownership check, before the command
    loops.retag_session_profile(session, "b", lambda s, p: (setattr(s, "profile", p), (p, "retagged"))[1])
    assert "another profile" in loops.run_loop_command("s1", "5m check", request_profile="default")
    assert _loop(agent, loops, home="b") is None  # profile b's store was never written


@requires_agent_modules
@pytest.mark.parametrize("rotations", [1, 2])
def test_compression_mid_wakeup_keeps_turn_and_loop(tmp_path, monkeypatch, rotations):
    from api import config
    session = NS(session_id="s1", profile=None, messages=[])
    agent, loops, started = _setup(tmp_path, monkeypatch, session)
    with loops._home(None):
        loops.run_due_loops()
        db = agent._get_session_db()
        db.create_session("s1", "webui")
        db.create_session("c1", "webui", parent_session_id="s1")
        assert agent.migrate_loop_to_session("s1", "c1")  # agent compressed; WebUI hasn't rotated yet
        cur = "c1"
        if rotations == 2:  # two rotations in one wakeup leave the turn record on the grandparent
            db.create_session("c2", "webui", parent_session_id="c1")
            assert agent.migrate_loop_to_session("c1", "c2")
            cur = "c2"
    monkeypatch.setattr(config, "ACTIVE_RUNS", {"stream1": {"session_id": "s1"}})  # turn still running
    loops.run_due_loops()
    s = _loop(agent, loops, cur)
    assert s.status == "active" and s.awaiting_response  # not cleared, not judged mid-turn
    monkeypatch.setattr(config, "ACTIVE_RUNS", {})
    session.session_id = cur
    session.messages.append({"role": "assistant", "content": "done\nLOOP_COMPLETE"})
    loops.run_due_loops()
    assert _loop(agent, loops, cur).status == "done"
    if rotations == 2:
        assert db.get_meta(loops._TURN_PREFIX + cur)  # re-keyed to the current session
    with loops._home(None):
        _due(agent, cur)
    loops.run_due_loops()
    assert len(started) == 1


@requires_agent_modules
def test_message_after_stop_does_not_hide_stop(tmp_path, monkeypatch):
    session = NS(session_id="s1", profile=None, messages=[])
    agent, loops, started = _setup(tmp_path, monkeypatch, session)
    loops.run_due_loops()
    session.messages += [{"role": "assistant", "content": "Task cancelled."},
                         {"role": "user", "content": "next question", "timestamp": 2000.0},
                         {"role": "assistant", "content": "answer"}]
    loops.run_due_loops()
    s = _loop(agent, loops)
    assert s.status == "paused" and s.paused_reason == "user-interrupted (Stop)"


def _deferred_stop_env(tmp_path, monkeypatch, session_source, worker_first):
    """Saved session in deferred save mode whose wakeup starts through the production chat-start preparer."""
    import queue
    from api import config, models, routes
    from api.models import Session
    monkeypatch.setattr(models, "SESSION_DIR", tmp_path / "sessions")
    (tmp_path / "sessions").mkdir()
    monkeypatch.setattr(routes, "get_webui_session_save_mode", lambda *a: "deferred")
    holder = {"s": Session(session_id="s1", title="loop", messages=[], session_source=session_source)}
    holder["s"].save()
    agent, loops, started = _setup(tmp_path, monkeypatch, holder["s"], get_session=lambda sid, **kw: holder["s"])

    def start(sid, msg, source):  # the prompt lives only in pending_* until the turn settles
        started.append(msg)
        routes._prepare_chat_start_session_for_stream(
            holder["s"], msg=msg, attachments=[], workspace=str(tmp_path), model="m",
            model_provider=None, stream_id="stream1", started_at=1000.75, source=source)
        config.STREAMS["stream1"], config.CANCEL_FLAGS["stream1"] = queue.Queue(), threading.Event()
        config.register_stream_owner("stream1", sid)
        if worker_first:
            config.register_session_writeback_owner(sid, "stream1")
        return {"stream_id": "stream1", "pending_started_at": 1000.75}

    monkeypatch.setattr(routes, "start_session_turn", start)
    return agent, loops, started, holder


@requires_agent_modules
@pytest.mark.parametrize("session_source,settled_by", [
    ("webui", "caller"), ("fork", "caller"), ("webui", "worker"), ("fork", "worker"),
])
def test_stop_on_wakeup_pauses_loop_after_reload(tmp_path, monkeypatch, session_source, settled_by):
    """Stop on a wakeup pauses the loop, whichever side persists the cancelled row first."""
    from api import config, streaming
    from api.models import Session
    from api.process_event_utils import build_active_turn_token
    agent, loops, started, holder = _deferred_stop_env(
        tmp_path, monkeypatch, session_source, worker_first=settled_by == "worker")
    with loops._home(None):
        _due(agent)
    loops.run_due_loops()
    assert holder["s"].messages == []  # deferred save: nothing written yet
    if session_source == "fork":
        assert holder["s"].pending_user_source == "fork"
    if settled_by == "worker":  # the streaming worker's cancel finalizer wins the race
        config.CANCEL_FLAGS["stream1"].set()
        streaming._finalize_cancelled_turn(holder["s"], stream_id="stream1")
        assert holder["s"].pending_user_message is None and holder["s"].active_stream_id is None
    assert streaming.cancel_stream("stream1")
    config.ACTIVE_RUNS.pop("stream1", None)
    holder["s"] = Session.load("s1")  # judge the durably settled transcript
    users = [m for m in holder["s"].messages if m.get("role") == "user"]
    assert len(users) == 1 and users[0]["_active_turn_token"] == build_active_turn_token("stream1", 1000.75)
    if settled_by == "caller":
        assert users[0]["timestamp"] == 1000.75  # Stop's own recovery keeps the exact pending time
    if session_source == "fork":
        assert users[0]["_source"] == "fork"
    loops.run_due_loops()
    s = _loop(agent, loops)
    assert s.status == "paused" and s.paused_reason == "user-interrupted (Stop)"
    with loops._home(None):
        _due(agent)
    loops.run_due_loops()
    assert len(started) == 1  # no further provider call after a forced due


@requires_agent_modules
@pytest.mark.parametrize("tail,status,reason", [
    ([{"role": "assistant", "content": "done\nLOOP_COMPLETE"}], "done", None),  # Steer, then complete
    ([{"role": "user", "content": "check", "_recovered": True, "_steered_tok": True},  # Steer, then Stop
      {"role": "assistant", "content": "Task cancelled."}], "paused", "user-interrupted (Stop)"),
])
def test_steer_during_wakeup_keeps_its_outcome(tmp_path, monkeypatch, tail, status, reason):
    """A typed Steer row (and Stop's same-token recovery row) must not cut the wakeup's outcome short."""
    session = NS(session_id="s1", profile=None, messages=[])
    agent, loops, started = _setup(tmp_path, monkeypatch, session)
    loops.run_due_loops()
    token = session.messages[0]["_active_turn_token"]
    session.messages.append({"role": "user", "content": "also check logs", "display_kind": "steer", "timestamp": 1500})
    for row in tail:
        row = dict(row)
        if row.pop("_steered_tok", False):
            row["_active_turn_token"] = token
        session.messages.append(row)
    loops.run_due_loops()
    s = _loop(agent, loops)
    assert (s.status, s.paused_reason) == (status, reason) and len(started) == 1
    assert not s.awaiting_response


def test_loop_scheduler_kill_switch(monkeypatch):
    from api import loops
    spawned = []
    monkeypatch.setattr(loops.threading, "Thread", lambda *a, **kw: spawned.append(kw) or NS(start=lambda: None))
    monkeypatch.setenv("HERMES_WEBUI_LOOP_SCHEDULER", "0")
    assert loops.start_loop_scheduler() is False and spawned == []
    monkeypatch.delenv("HERMES_WEBUI_LOOP_SCHEDULER")
    assert loops.start_loop_scheduler() is True and spawned[0]["name"] == "webui-loop-scheduler"


def test_loop_state_helpers_ignore_old_rows_and_parent_cycles():
    from api import loops
    # A token-less row with the same prompt must not shadow the real wakeup turn.
    turn = {"token": "tok", "started_at": 1000.75, "prompt": "check"}
    msgs = [{"role": "user", "content": "check", "timestamp": 1000},
            {"role": "assistant", "content": "old reply"},
            {"role": "user", "content": "check", "timestamp": 1000.75, "_active_turn_token": "tok"},
            {"role": "assistant", "content": "done\nLOOP_COMPLETE"}]
    assert loops._wakeup_outcome(msgs, turn) == (False, "done\nLOOP_COMPLETE")
    assert loops._wakeup_outcome(msgs[:2], turn) == (False, "")
    # A compression parent cycle in the turn record must terminate instead of looping.
    parents = {"a": "b", "b": "a"}
    db = NS(get_meta=lambda k: None, get_session=lambda sid: {"parent_session_id": parents.get(sid)},
            set_meta=lambda k, v: (_ for _ in ()).throw(AssertionError("no record to copy")))
    assert loops._turn_record(db, "a") == {}


def test_launch_response_keeps_started_at_when_worker_clears_pending(issue7193_env, tmp_path, monkeypatch):
    # greptile 4237618938: the launch timestamp must survive the worker (or Stop) clearing pending_*.
    from api import routes
    from api.models import new_session
    cleared = threading.Event()

    def worker(*_a, **_kw):
        session.pending_started_at = None
        session.pending_user_message = None
        cleared.set()

    monkeypatch.setattr(routes, "_run_agent_streaming", worker)
    # runs after the session lock is released, before the response is built: let the worker win
    monkeypatch.setattr(routes, "set_last_workspace", lambda *_a, **_kw: cleared.wait(2))
    session = new_session(workspace=str(tmp_path / "workspace"))
    resp = _start7193(session, workspace=tmp_path / "workspace")
    assert cleared.wait(2) and session.pending_started_at is None
    assert resp["pending_started_at"]  # captured under the lock, not re-read after the worker ran


@requires_agent_modules
def test_archived_session_loop_pauses_instead_of_firing(tmp_path, monkeypatch):
    session = NS(session_id="s1", profile=None, messages=[], archived=True)
    agent, loops, started = _setup(tmp_path, monkeypatch, session)
    loops.run_due_loops()
    s = _loop(agent, loops)
    assert started == [] and s.status == "paused" and s.paused_reason == "session archived"


@requires_agent_modules
def test_archive_guard_pauses_loop_under_loop_lock(tmp_path, monkeypatch):
    session = NS(session_id="s1", profile=None, messages=[], archived=False)
    agent, loops, _ = _setup(tmp_path, monkeypatch, session)
    with loops.archive_guard("s1") as guard:
        assert loops._session_lock("s1").locked()  # held before the caller takes the agent lock
        session.archived, guard["session"] = True, session
    assert _loop(agent, loops).paused_reason == "session archived"
    with loops.archive_guard("s1") as guard:  # unarchive does not touch the loop
        session.archived, guard["session"] = False, session
    assert _loop(agent, loops).status == "paused"


def test_loop_js_wiring_is_session_scoped_and_chat_bound():
    """Only `/loop` carries a session_id, and a busy `/loop` reply lands in the chat that asked."""
    body = _src("static/commands.js")
    body = body[body.index("async function _runAgentCommandTransport"):]
    body = body[:body.index("\n}\n")]
    assert "/^\\/?loop(\\s|$)/i.test(transportCommand)?{command:transportCommand,session_id:" in body and ":{command:transportCommand})" in body
    busy = _src("static/messages.js")
    busy = busy[busy.index("const _ownSid="):busy.index("const defaultMessageMode=")]
    assert "executeAgentCommand(text,_pcCmd||{name:'loop'})" in busy and "_ownMsgs=S.messages" in busy
    # The advertised alias (/proactive) resolves through the registry (envelope .command) before the busy check.
    assert "getAgentCommandMetadata(_pc.name)" in busy and "(_pcCmd&&_pcCmd.name||_pc.name)==='loop'" in busy
    assert '("/loop", "loop", "/proactive", "proactive")' in _src("api/routes.py")  # raw /api/commands/exec too
    assert "_ownMsgs.push({role:'assistant'" in busy and "S.messages.push({role:'assistant'" not in busy
    assert "S.messages===_ownMsgs&&(S.session&&S.session.session_id)===_ownSid" in busy
