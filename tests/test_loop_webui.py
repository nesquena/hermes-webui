from types import SimpleNamespace as NS

from tests.conftest import requires_agent_modules


@requires_agent_modules
def test_webui_loop_matches_cli(tmp_path, monkeypatch):
    from hermes_cli import goals, loops as agent
    from api import background_process, loops, models, profiles, routes
    msgs, started = [], []
    monkeypatch.setattr(goals, "_DB_CACHE", {})
    monkeypatch.setattr(profiles, "get_hermes_home_for_profile", lambda p: tmp_path)
    monkeypatch.setattr(profiles, "_profiles_root", lambda: tmp_path / "none")
    monkeypatch.setattr(models, "get_session", lambda sid, **kw: NS(profile=None, messages=msgs))
    monkeypatch.setattr(background_process, "_session_has_active_turn", lambda sid: False)
    monkeypatch.setattr(routes, "start_session_turn",
                        lambda sid, m, source: started.append(m) or {"pending_started_at": float(len(started))})

    def tick(reply, max_ticks=100):  # a due tick fires, its turn ends with `reply`, the scheduler judges it
        s = agent.load_loop("s1")
        s.next_due_at, s.max_ticks = 0, max_ticks
        agent.save_loop("s1", s)
        loops.run_due_loops()
        msgs.extend([{"role": "user", "content": started[-1], "timestamp": float(len(started))},
                     {"role": "assistant", "content": reply}])
        loops.run_due_loops()
        return agent.load_loop("s1")

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
    from hermes_cli import goals, loops as agent
    from api import background_process, loops, models, profiles, routes
    msgs, started = [{"role": "assistant", "content": "old\nLOOP_COMPLETE"}], []
    monkeypatch.setattr(goals, "_DB_CACHE", {})
    monkeypatch.setattr(profiles, "get_hermes_home_for_profile", lambda p: tmp_path)
    monkeypatch.setattr(profiles, "_profiles_root", lambda: tmp_path / "none")
    monkeypatch.setattr(background_process, "_session_has_active_turn", lambda sid: False)

    def get_session(sid, **kw):
        if sid != "s1":
            raise KeyError(sid)
        return NS(profile=None, messages=msgs)

    monkeypatch.setattr(models, "get_session", get_session)
    # Unknown session: fail closed instead of writing the default profile's state.db.
    assert loops.run_loop_command("gone", "5m x") == "/loop: open a saved chat first."
    with loops._home(None):
        assert agent.load_loop("gone") is None

    def due():
        s = agent.load_loop("s1")
        s.next_due_at = 0
        agent.save_loop("s1", s)

    loops.run_loop_command("s1", "5m check")

    def boom(*a, **kw):
        raise RuntimeError("start failed")

    monkeypatch.setattr(routes, "start_session_turn", boom)
    with loops._home(None):
        due()
        loops.run_due_loops()
        s = agent.load_loop("s1")  # a start that raised is rolled back, never judged later
        assert s.ticks_fired == 0 and not s.awaiting_response and s.status == "active"
        monkeypatch.setattr(routes, "start_session_turn", lambda sid, m, source: started.append(m) or {})
        due()
        loops.run_due_loops()
        msgs.append({"role": "user", "content": started[-1]})  # wakeup turn ended with no reply
        loops.run_due_loops()
        s = agent.load_loop("s1")  # the older LOOP_COMPLETE reply is not this tick's reply
        assert s.status == "active" and s.ticks_fired == 1 and not s.awaiting_response


def test_loop_controls_bypass_busy_routing():
    from pathlib import Path
    src = (Path(__file__).resolve().parents[1] / "static" / "messages.js").read_text(encoding="utf-8")
    busy = src[src.index("Busy-control slash commands must be intercepted"):src.index("const defaultMessageMode=")]
    assert "_pc.name==='loop'" in busy and "executeAgentCommand(text,{name:'loop'})" in busy
