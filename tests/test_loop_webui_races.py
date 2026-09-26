import threading
from types import SimpleNamespace as NS

from tests.conftest import requires_agent_modules


def _setup(tmp_path, monkeypatch, session):
    from hermes_cli import goals, loops as agent
    from api import background_process, loops, models, profiles, routes
    from api.process_event_utils import build_active_turn_token
    started = []
    monkeypatch.setattr(goals, "_DB_CACHE", {})
    monkeypatch.setattr(profiles, "get_hermes_home_for_profile", lambda p: tmp_path / (p or "default"))
    monkeypatch.setattr(profiles, "_profiles_root", lambda: tmp_path / "none")
    monkeypatch.setattr(models, "get_session", lambda sid, **kw: session)
    monkeypatch.setattr(background_process, "_session_has_active_turn", lambda sid: False)

    def start(sid, msg, source):  # records the wakeup user row the way _start_run stamps it
        started.append(msg)
        sid_, at = f"stream{len(started)}", 1000.0 + len(started)
        session.messages.append({"role": "user", "content": msg, "_source": source, "timestamp": at,
                                 "_active_turn_token": build_active_turn_token(sid_, at)})
        return {"stream_id": sid_, "pending_started_at": at}

    monkeypatch.setattr(routes, "start_session_turn", start)
    loops.run_loop_command("s1", "5m check")
    return agent, loops, started


def _due(agent):
    s = agent.load_loop("s1")
    s.next_due_at = 0
    agent.save_loop("s1", s)


@requires_agent_modules
def test_retag_between_load_and_launch_never_runs_other_profile(tmp_path, monkeypatch):
    session = NS(session_id="s1", profile=None, messages=[])
    agent, loops, started = _setup(tmp_path, monkeypatch, session)
    with loops._home(None):
        _due(agent)

    def retag(sid):  # chat start retags the empty session to profile b after the scheduler listed it
        session.profile = "b"
        return False

    monkeypatch.setattr(agent, "goal_blocks_loop_tick", retag)
    loops.run_due_loops()
    assert started == []
    with loops._home(None):
        assert agent.load_loop("s1").status == "cleared"  # the loop left in profile a is dropped


@requires_agent_modules
def test_retag_helper_clears_old_profile_loop(tmp_path, monkeypatch):
    session = NS(session_id="s1", profile=None, messages=[])
    agent, loops, started = _setup(tmp_path, monkeypatch, session)
    loops.retag_session_profile(session, "b")
    assert session.profile == "b"
    with loops._home(None):
        assert agent.load_loop("s1").status == "cleared"
    from pathlib import Path
    src = (Path(__file__).resolve().parents[1] / "api" / "routes.py").read_text(encoding="utf-8")
    assert src.count("retag_session_profile(s, requested_profile)") == 2
    assert "s.profile = requested_profile" not in src


@requires_agent_modules
def test_stop_from_another_writer_is_not_overwritten(tmp_path, monkeypatch):
    session = NS(session_id="s1", profile=None, messages=[])
    agent, loops, started = _setup(tmp_path, monkeypatch, session)
    with loops._home(None):
        _due(agent)

    def cli_stop(sid):  # another process (CLI) stops the loop after the scheduler loaded it
        agent.clear_loop(sid)
        return False

    monkeypatch.setattr(agent, "goal_blocks_loop_tick", cli_stop)
    loops.run_due_loops()
    assert started == []
    with loops._home(None):
        assert agent.load_loop("s1").status == "cleared"


@requires_agent_modules
def test_stop_command_waits_for_scheduler_pass(tmp_path, monkeypatch):
    session = NS(session_id="s1", profile=None, messages=[])
    agent, loops, started = _setup(tmp_path, monkeypatch, session)
    with loops._home(None):
        _due(agent)
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
    assert out == ["✓ Loop stopped."]
    with loops._home(None):
        assert agent.load_loop("s1").status == "cleared"
    loops.run_due_loops()
    assert len(started) == 1  # never brought back after stop reported success


@requires_agent_modules
def test_user_turn_before_judgment_does_not_decide_tick(tmp_path, monkeypatch):
    session = NS(session_id="s1", profile=None, messages=[])
    agent, loops, started = _setup(tmp_path, monkeypatch, session)
    with loops._home(None):
        _due(agent)
    loops.run_due_loops()
    session.messages += [{"role": "assistant", "content": "still deploying"},
                         {"role": "user", "content": "unrelated", "timestamp": 2000.0},
                         {"role": "assistant", "content": "sure\nLOOP_COMPLETE"}]
    loops.run_due_loops()
    with loops._home(None):
        s = agent.load_loop("s1")
    assert s.status == "active" and not s.awaiting_response and s.ticks_fired == 1


def test_retag_without_agent_modules_still_retags(monkeypatch):
    import builtins
    from types import SimpleNamespace
    from api import loops
    real = builtins.__import__

    def no_agent(name, *a, **kw):
        if name.startswith("hermes_cli"):
            raise ImportError(name)
        return real(name, *a, **kw)

    monkeypatch.setattr(builtins, "__import__", no_agent)
    s = SimpleNamespace(session_id="s1", profile=None)
    loops.retag_session_profile(s, "b")
    assert s.profile == "b"


@requires_agent_modules
def test_loop_command_rechecks_profile_under_retag_lock(tmp_path, monkeypatch):
    session = NS(session_id="s1", profile=None, messages=[])
    agent, loops, started = _setup(tmp_path, monkeypatch, session)
    loops.run_loop_command("s1", "stop")
    loops.retag_session_profile(session, "b")  # lands after the route's ownership check, before the command
    out = loops.run_loop_command("s1", "5m check", request_profile="default")
    assert "another profile" in out
    with loops._home("b"):
        assert agent.load_loop("s1") is None  # profile b's store was never written
    from pathlib import Path
    src = (Path(__file__).resolve().parents[1] / "api" / "routes.py").read_text(encoding="utf-8")
    assert "request_profile=_get_active_profile_name())" in src


@requires_agent_modules
def test_compression_mid_wakeup_keeps_turn_and_loop(tmp_path, monkeypatch):
    from api import config
    session = NS(session_id="s1", profile=None, messages=[])
    agent, loops, started = _setup(tmp_path, monkeypatch, session)
    with loops._home(None):
        _due(agent)
        loops.run_due_loops()
        db = agent._get_session_db()
        db.create_session("s1", "webui")
        db.create_session("c1", "webui", parent_session_id="s1")
        assert agent.migrate_loop_to_session("s1", "c1")  # agent compressed; WebUI hasn't rotated yet
    monkeypatch.setattr(config, "ACTIVE_RUNS", {"stream1": {"session_id": "s1"}})  # turn still running
    loops.run_due_loops()
    with loops._home(None):
        s = agent.load_loop("c1")
    assert s.status == "active" and s.awaiting_response  # not cleared, not judged mid-turn
    monkeypatch.setattr(config, "ACTIVE_RUNS", {})
    session.session_id = "c1"
    session.messages.append({"role": "assistant", "content": "done\nLOOP_COMPLETE"})
    loops.run_due_loops()
    with loops._home(None):
        assert agent.load_loop("c1").status == "done"


@requires_agent_modules
def test_message_after_stop_does_not_hide_stop(tmp_path, monkeypatch):
    session = NS(session_id="s1", profile=None, messages=[])
    agent, loops, started = _setup(tmp_path, monkeypatch, session)
    with loops._home(None):
        _due(agent)
    loops.run_due_loops()
    session.messages += [{"role": "assistant", "content": "Task cancelled."},
                         {"role": "user", "content": "next question", "timestamp": 2000.0},
                         {"role": "assistant", "content": "answer"}]
    loops.run_due_loops()
    with loops._home(None):
        s = agent.load_loop("s1")
    assert s.status == "paused" and s.paused_reason == "user-interrupted (Stop)"


@requires_agent_modules
def test_stop_on_wakeup_via_real_cancel_pauses_loop(tmp_path, monkeypatch):
    import queue
    from api import config, routes, streaming
    from api.models import Session
    session = Session(session_id="s1", title="loop", messages=[])
    session.save = lambda *a, **kw: None
    agent, loops, started = _setup(tmp_path, monkeypatch, session)
    monkeypatch.setattr(streaming, "get_session", lambda sid, **kw: session)

    def start(sid, msg, source):  # deferred save mode: the prompt lives only in pending_* until the turn ends
        started.append(msg)
        session.active_stream_id, session.pending_user_message = "stream1", msg
        session.pending_started_at, session.pending_user_source = 1000.75, source
        config.STREAMS["stream1"], config.CANCEL_FLAGS["stream1"] = queue.Queue(), threading.Event()
        config.register_stream_owner("stream1", sid)
        return {"stream_id": "stream1", "pending_started_at": 1000.75}

    monkeypatch.setattr(routes, "start_session_turn", start)
    with loops._home(None):
        _due(agent)
    loops.run_due_loops()
    assert streaming.cancel_stream("stream1")
    user = [m for m in session.messages if m.get("role") == "user"][-1]
    assert user["timestamp"] == 1000 and "_active_turn_token" not in user  # the recovered row
    config.ACTIVE_RUNS.pop("stream1", None)
    loops.run_due_loops()
    with loops._home(None):
        s = agent.load_loop("s1")
    assert s.status == "paused" and s.paused_reason == "user-interrupted (Stop)"
