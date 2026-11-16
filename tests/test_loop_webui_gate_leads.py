"""/loop gate leads: tombstoned profile dirs, Stop after Steer, server-settled cancels, admission, judge env."""
from types import SimpleNamespace as NS

import pytest

from tests.conftest import requires_agent_modules
from tests.test_cancel_restart_journal_recovery import (
    _isolated_state,  # noqa: F401 - imported autouse isolation fixture
    _start_cancelled_turn,
)
from tests.test_loop_webui import _due, _loop, _setup


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
    from api.streaming import CANCEL_INITIATOR_SERVER, _cancelled_turn_content
    session = NS(session_id="s1", profile=None, messages=[])
    agent, loops, started = _setup(tmp_path, monkeypatch, session)
    loops.run_due_loops()
    session.messages.append({"role": "assistant", "_error": True, "cancel_initiator": CANCEL_INITIATOR_SERVER,
                             "content": _cancelled_turn_content("Server restarting.", initiator="server")})
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
            db.set_meta(loops._TURN_PREFIX + "s1", json.dumps(record))
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
