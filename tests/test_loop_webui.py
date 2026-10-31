from pathlib import Path
from types import SimpleNamespace as NS

from tests.conftest import requires_agent_modules


def _src(rel):
    return (Path(__file__).resolve().parents[1] / rel).read_text(encoding="utf-8")


def _due(agent, sid="s1"):
    s = agent.load_loop(sid)
    s.next_due_at = 0
    agent.save_loop(sid, s)


def _loop(agent, loops, sid="s1", home=None):
    """The stored loop state, read in `home`'s profile store."""
    with loops._home(home):
        return agent.load_loop(sid)


def _setup(tmp_path, monkeypatch, session, cmd="5m check", due=True, get_session=None, home=None, root=None):
    """Sandboxed loop store for one session; `cmd` starts its loop and `due` makes that loop due."""
    from hermes_cli import goals, loops as agent
    from api import background_process, loops, models, profiles, routes, streaming
    from api.process_event_utils import build_active_turn_token
    started = []
    monkeypatch.setattr(goals, "_DB_CACHE", {})
    monkeypatch.setattr(profiles, "get_hermes_home_for_profile", home or (lambda p: tmp_path / (p or "default")))
    monkeypatch.setattr(profiles, "_profiles_root", lambda: root or tmp_path / "none")
    monkeypatch.setattr(background_process, "_session_has_active_turn", lambda sid: False)
    for mod in (models, streaming):
        monkeypatch.setattr(mod, "get_session", get_session or (lambda sid, **kw: session))

    def start(sid, msg, source):  # stamps the wakeup user row the way _start_run does
        started.append(msg)
        stream, at = f"stream{len(started)}", 1000.0 + len(started)
        session.messages.append({"role": "user", "content": msg, "_source": source, "timestamp": at,
                                 "_active_turn_token": build_active_turn_token(stream, at)})
        return {"stream_id": stream, "pending_started_at": at}

    monkeypatch.setattr(routes, "start_session_turn", start)
    if cmd:
        loops.run_loop_command("s1", cmd)
    if due:
        with loops._home(None):
            _due(agent)
    return agent, loops, started


@requires_agent_modules
def test_webui_loop_matches_cli(tmp_path, monkeypatch):
    from hermes_cli import goals
    session = NS(session_id="s1", profile=None, messages=[])
    agent, loops, started = _setup(tmp_path, monkeypatch, session, cmd="", due=False)

    def tick(reply, max_ticks=100):  # a due tick fires, its turn ends with `reply`, the scheduler judges it
        with loops._home(None):
            s = agent.load_loop("s1")
            s.next_due_at, s.max_ticks = 0, max_ticks
            agent.save_loop("s1", s)
        loops.run_due_loops()
        session.messages.append({"role": "assistant", "content": reply})
        loops.run_due_loops()
        return _loop(agent, loops)

    out = loops.run_loop_command("s1", "5m check the deploy")
    with loops._home(None):
        assert out == agent.dispatch_loop_command(agent.LoopManager(session_id="c1"), "5m check the deploy")["output"]
        assert "slash" in loops.run_loop_command("s1", "10m /recap")
        assert tick("still rolling").next_due_at > 0 and started[0].startswith("[/loop wakeup #1, every 5m]")
        assert "1/100 budget" in loops.run_loop_command("s1", "status")
        assert tick("still rolling", max_ticks=2).paused_reason == "tick budget exhausted (2/2)"  # pause, as CLI
        loops.run_loop_command("s1", "resume")
        assert tick("Task cancelled.").paused_reason == "user-interrupted (Stop)"  # CLI Ctrl+C parity
        loops.run_loop_command("s1", "resume")
        assert tick("done\nLOOP_COMPLETE").status == "done" and len(started) == 4
        for cmd in ("5m poll CI --times 2", "5m watch the queue --until queue is empty"):  # CLI flags, CLI text
            loops.run_loop_command("s1", "stop")
            agent.LoopManager(session_id="c1").clear()
            assert loops.run_loop_command("s1", cmd) == agent.dispatch_loop_command(
                agent.LoopManager(session_id="c1"), cmd)["output"]
        monkeypatch.setattr(goals, "judge_goal", lambda goal, reply, **kw: ("done", f"{goal}: yes", False, None, False))
        s = tick("queue drained")
        assert "Stop condition: queue is empty" in started[-1] and s.status == "done"
        assert s.last_stop_reason == "stop condition met: queue is empty: yes"
        loops.run_loop_command("s1", "5m poll CI --times 2")
        assert "0/2 runs" in loops.run_loop_command("s1", "status")
        assert tick("a").status == "active" and tick("b").last_stop_reason == "completed the requested 2 runs"


@requires_agent_modules
def test_webui_loop_greptile_regressions(tmp_path, monkeypatch):
    from api import routes
    session = NS(session_id="s1", profile=None, messages=[{"role": "assistant", "content": "old\nLOOP_COMPLETE"}])

    def find(sid, **kw):
        if sid != "s1":
            raise KeyError(sid)
        return session

    agent, loops, started = _setup(tmp_path, monkeypatch, session, cmd="", due=False, get_session=find)
    # Unknown session: fail closed instead of writing the default profile's state.db.
    assert loops.run_loop_command("gone", "5m x") == "/loop: open a saved chat first."
    assert _loop(agent, loops, "gone") is None
    loops.run_loop_command("s1", "5m check")

    def boom(*a, **kw):
        raise RuntimeError("start failed")

    monkeypatch.setattr(routes, "start_session_turn", boom)
    with loops._home(None):
        _due(agent)
        loops.run_due_loops()
        s = agent.load_loop("s1")  # a start that raised is rolled back, never judged later
        assert s.ticks_fired == 0 and not s.awaiting_response and s.status == "active"
        monkeypatch.setattr(routes, "start_session_turn", lambda sid, m, source: started.append(m) or {})
        _due(agent)
        loops.run_due_loops()
        session.messages.append({"role": "user", "content": started[-1]})  # wakeup turn ended with no reply
        loops.run_due_loops()
        s = agent.load_loop("s1")  # the older LOOP_COMPLETE reply is not this tick's reply
        assert s.status == "active" and s.ticks_fired == 1 and not s.awaiting_response


@requires_agent_modules
def test_isolated_profile_loop_launches_without_scanning_siblings(tmp_path, monkeypatch):
    from api import profiles
    (tmp_path / "profiles" / "bob").mkdir(parents=True)
    homes = []
    session = NS(session_id="s1", profile="alice", messages=[])
    agent, loops, started = _setup(tmp_path, monkeypatch, session, due=False, root=tmp_path / "profiles",
                                   home=lambda p: homes.append(p) or tmp_path)
    monkeypatch.setattr(profiles, "_is_isolated_profile_mode", lambda: True)
    monkeypatch.setattr(profiles, "_isolated_profile_name", lambda: "alice")
    with loops._home("alice"):
        _due(agent)
    homes.clear()
    loops.run_due_loops()
    assert _loop(agent, loops, home="alice").status == "active"
    assert len(started) == 1 and homes[0] == "alice" and None not in homes and "bob" not in homes
