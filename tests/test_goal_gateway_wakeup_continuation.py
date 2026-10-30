"""Regression tests for /goal continuation across Gateway turns, subagent wakeups, and resume."""

from __future__ import annotations

from collections import OrderedDict
from types import SimpleNamespace

import pytest

from api import gateway_chat, goals as webui_goals, models, routes
from api.config import PENDING_GOAL_CONTINUATION, STREAMS, create_stream_channel
from api.models import new_session


@pytest.fixture(autouse=True)
def _clean_continuation_state():
    """No test may leak a pending record or a grace timer into the next."""
    yield
    from api.config import PENDING_GOAL_CONTINUATION_PROMPTS

    for sid in set(PENDING_GOAL_CONTINUATION) | set(PENDING_GOAL_CONTINUATION_PROMPTS):
        webui_goals.clear_pending_goal_continuation(sid)


@pytest.mark.parametrize("wakeup_source", ["process_wakeup", "delegation_wakeup"])
def test_start_session_turn_marks_wakeup_goal_related_when_goal_active(
    monkeypatch, tmp_path, wakeup_source
):
    """A subagent/process wakeup on a session with an active goal must run with goal_related=True."""
    session = SimpleNamespace(
        session_id="sid-wakeup-goal",
        profile="deepseek",
        workspace=str(tmp_path),
        model="gpt-5.5",
        model_provider="openai-codex",
        process_wakeup_pause=None,
        save=lambda **_kw: None,
    )
    PENDING_GOAL_CONTINUATION.discard(session.session_id)

    monkeypatch.setattr(routes, "get_session", lambda _sid: session)
    monkeypatch.setattr(routes, "_resolve_chat_workspace_with_recovery", lambda _s, _w: str(tmp_path))
    monkeypatch.setattr(routes, "_read_profile_model_config", lambda _s, _p: ("openai-codex", "gpt-5.5", {}))
    monkeypatch.setattr(
        routes,
        "_resolve_compatible_session_model_state",
        lambda model, provider, **_kw: (model, provider, False),
    )
    monkeypatch.setattr(routes, "clear_process_wakeup_pause_if_model_changed", lambda *_a, **_kw: False)
    monkeypatch.setattr(routes, "process_wakeup_pause_matches", lambda *_a, **_kw: False)
    monkeypatch.setattr(routes, "suppress_process_wakeup_for_provider_pause", lambda *_a, **_kw: None)
    monkeypatch.setattr(webui_goals, "has_active_goal", lambda sid, **_kw: sid == "sid-wakeup-goal")

    captured = {}

    def fake_start_run(s, **kwargs):
        captured.update(kwargs)
        return {"stream_id": "wakeup-stream", "session_id": s.session_id, "_status": 200}

    monkeypatch.setattr(routes, "_start_run", fake_start_run)

    resp = routes.start_session_turn(
        session.session_id,
        "[IMPORTANT: A delegated subagent finished.]",
        source=wakeup_source,
    )
    assert resp["_status"] == 200
    assert captured["source"] == wakeup_source
    assert captured["goal_related"] is True


def _goal_command_env(monkeypatch, tmp_path, session_id):
    class FakeState:
        goal = "replace helpers with native code"
        status = "active"
        turns_used = 0
        max_turns = 100
        last_verdict = None
        last_reason = None
        paused_reason = None

    holder = {"state": FakeState()}

    class FakeGoalManager:
        def __init__(self, session_id, default_max_turns=20):
            self.session_id = session_id

        @property
        def state(self):
            return holder["state"]

        def is_active(self):
            return holder["state"] is not None and holder["state"].status == "active"

        def has_goal(self):
            return holder["state"] is not None

        def resume(self, reset_budget=True):
            holder["state"].status = "active"
            return holder["state"]

        def pause(self, reason="user-paused"):
            holder["state"].status = "paused"
            holder["state"].paused_reason = reason
            return holder["state"]

        def clear(self):
            holder["state"] = None

        def next_continuation_prompt(self):
            st = holder["state"]
            if st is None or st.status != "active":
                return None
            return f"[Continuing toward your standing goal]\nGoal: {st.goal}"

    session = SimpleNamespace(
        session_id=session_id,
        profile="deepseek",
        workspace=str(tmp_path),
        model="gpt-5.5",
        model_provider="openai-codex",
        active_stream_id=None,
    )
    monkeypatch.setattr(webui_goals, "GoalManager", FakeGoalManager)
    monkeypatch.setattr(routes, "get_session", lambda _sid: session)
    monkeypatch.setattr(routes, "j", lambda _h, payload, status=200, **_kw: {"status": status, "payload": payload})
    monkeypatch.setattr(routes, "resolve_trusted_workspace", lambda ws, **_kw: ws)
    monkeypatch.setattr(routes, "_read_profile_model_config", lambda _s, _p: ("openai-codex", "gpt-5.5", {}))
    monkeypatch.setattr(
        routes,
        "_resolve_compatible_session_model_state",
        lambda model, provider, **_kw: (model, provider, False),
    )
    monkeypatch.setattr(routes, "webui_gateway_chat_enabled", lambda _cfg: False)
    kickoffs = []

    def fake_start_run(s, **kwargs):
        kickoffs.append(kwargs)
        return {"stream_id": "resume-kickoff", "session_id": s.session_id, "_status": 200}

    monkeypatch.setattr(routes, "_start_run", fake_start_run)
    return session, holder, kickoffs


def test_goal_pause_resume_keep_record_and_clear_retires_it(monkeypatch, tmp_path):
    """Pause keeps the pending record, resume keeps it, clear retires it."""
    session, _holder, kickoffs = _goal_command_env(monkeypatch, tmp_path, "sid-goal-resume")
    webui_goals.clear_pending_goal_continuation(session.session_id)
    cid = webui_goals.register_pending_goal_continuation(session.session_id, "continue the goal")
    assert routes._handle_goal_command(object(), {"session_id": session.session_id, "args": "pause"})["status"] == 200
    assert webui_goals.peek_pending_goal_continuation_id(session.session_id) == cid
    assert routes._handle_goal_command(object(), {"session_id": session.session_id, "args": "resume"})["status"] == 200
    assert webui_goals.peek_pending_goal_continuation_id(session.session_id) == cid
    assert kickoffs == [], "resume must not double-arm when a record is pending"
    receipt = webui_goals.consume_pending_goal_continuation(session.session_id, cid)
    assert isinstance(receipt, dict) and receipt["continuation_id"] == cid
    assert webui_goals.restore_pending_goal_continuation(session.session_id, receipt)
    assert routes._handle_goal_command(object(), {"session_id": session.session_id, "args": "clear"})["status"] == 200
    assert webui_goals.peek_pending_goal_continuation_id(session.session_id) is None
    assert session.session_id not in PENDING_GOAL_CONTINUATION


def test_goal_resume_without_pending_record_kicks_off_goal_related_turn(monkeypatch, tmp_path):
    """Resume with no pending record re-arms the loop with a goal-related kickoff."""
    session, holder, kickoffs = _goal_command_env(monkeypatch, tmp_path, "sid-goal-resume-kick")
    webui_goals.clear_pending_goal_continuation(session.session_id)
    holder["state"].status = "paused"
    res = routes._handle_goal_command(object(), {"session_id": session.session_id, "args": "resume"})
    assert res["status"] == 200
    assert res["payload"]["stream_id"] == "resume-kickoff"
    assert len(kickoffs) == 1
    assert kickoffs[0]["goal_related"] is True
    assert "replace helpers with native code" in kickoffs[0]["msg"]
    assert session.session_id not in PENDING_GOAL_CONTINUATION


def test_evaluate_goal_after_turn_forwards_background_processes_and_active_delegations(
    monkeypatch, tmp_path
):
    """evaluate_goal_after_turn passes live background_processes and active_delegations to the manager."""
    captured = {}

    class FakeState:
        goal = "ship all waves"
        status = "active"
        turns_used = 1
        max_turns = 20
        last_turn_at = 0.0

    class FakeGoalManager:
        def __init__(self, session_id, default_max_turns=20):
            self.session_id = session_id
            self.state = FakeState()

        def is_active(self):
            return True

        def evaluate_after_turn(
            self,
            last_response,
            *,
            user_initiated=True,
            background_processes=None,
            active_delegations=0,
        ):
            captured["last_response"] = last_response
            captured["user_initiated"] = user_initiated
            captured["background_processes"] = background_processes
            captured["active_delegations"] = active_delegations
            return {
                "status": "active",
                "should_continue": False,
                "continuation_prompt": None,
                "verdict": "wait",
                "reason": "waiting on 16 subagents",
                "message": "⏳ Goal parked (judge) — waiting on 300s: waiting on 16 subagents",
            }

    monkeypatch.setattr(webui_goals, "_manager", lambda _sid, **_kw: FakeGoalManager(_sid))
    monkeypatch.setattr(
        webui_goals,
        "_gather_goal_turn_background_state",
        lambda _sid, **_kw: ([{"session_id": "proc_1", "status": "running"}], 16),
    )

    decision = webui_goals.evaluate_goal_after_turn(
        "sid-deleg",
        "Spawned 16 subagents; ending turn.",
        user_initiated=True,
        profile_home=tmp_path,
    )
    assert captured == {
        "last_response": "Spawned 16 subagents; ending turn.",
        "user_initiated": True,
        "background_processes": [{"session_id": "proc_1", "status": "running"}],
        "active_delegations": 16,
    }
    assert decision["verdict"] == "wait"
    assert decision["should_continue"] is False


def _isolated_sessions(tmp_path, monkeypatch):
    """Point the session store at *tmp_path* so tests cannot share state."""
    session_dir = tmp_path / "sessions"
    session_dir.mkdir(exist_ok=True)
    monkeypatch.setattr(models, "SESSION_DIR", session_dir)
    monkeypatch.setattr(models, "SESSION_INDEX_FILE", session_dir / "_index.json")
    monkeypatch.setattr(models, "SESSIONS", OrderedDict())


def _gateway_response(monkeypatch):
    """Fake Gateway SSE response for one turn."""

    class FakeResponse:
        def __enter__(self):
            return self

        def __exit__(self, *_a):
            return False

        def __iter__(self):
            yield b'data: {"choices":[{"delta":{"content":"turn done"}}]}\n\n'
            yield b"data: [DONE]\n\n"

    monkeypatch.setenv("HERMES_WEBUI_GATEWAY_BASE_URL", "http://gateway.local")
    monkeypatch.setattr(
        gateway_chat.urllib.request, "urlopen", lambda req, timeout=0: FakeResponse()
    )


def _gateway_in_turn_goal_env(tmp_path, monkeypatch, *, stream_id):
    """Session with a fake native manager that already evaluated this turn's goal."""
    _isolated_sessions(tmp_path, monkeypatch)
    _gateway_response(monkeypatch)

    import time as _time

    eval_calls = []

    class FakeState:
        goal = "remove all replaceable helpers"
        status = "active"
        turns_used = 1
        max_turns = 100
        last_verdict = "continue"
        last_reason = "Go fell only 2.4%; another wave is needed."
        paused_reason = None
        last_turn_at = _time.time() + 60.0

    class FakeMgr:
        def __init__(self):
            self.state = FakeState()

        def is_active(self):
            return True

        def is_waiting(self):
            return False

        def next_continuation_prompt(self):
            return f"[Continuing toward your standing goal]\nGoal: {self.state.goal}"

        def evaluate_after_turn(self, *_a, **_kw):
            eval_calls.append(True)
            raise AssertionError("mgr.evaluate_after_turn must not be called a second time")

    fake_mgr = FakeMgr()
    (tmp_path / "state.db").write_bytes(b"sqlite")
    monkeypatch.setattr(webui_goals, "_NativeGoalManager", FakeMgr)
    monkeypatch.setattr(webui_goals, "GoalManager", FakeMgr)
    monkeypatch.setattr(webui_goals, "_manager", lambda _sid, **_kw: fake_mgr)
    monkeypatch.setattr("api.profiles.get_hermes_home_for_profile", lambda _p: tmp_path)

    s = new_session()
    s.active_stream_id = stream_id
    s.pending_user_message = "continue - subagents have finished"
    s.pending_attachments = []
    s.pending_started_at = 123
    s.save()
    channel = create_stream_channel()
    STREAMS[stream_id] = channel
    PENDING_GOAL_CONTINUATION.discard(s.session_id)
    return s, channel, fake_mgr, eval_calls


def test_gateway_worker_adopts_in_turn_gateway_goal_evaluation_without_double_judging(
    tmp_path, monkeypatch
):
    """An in-turn Gateway evaluation is adopted once, without a second judge call."""
    stream_id = "stream-gateway-in-turn-goal"
    s, channel, fake_mgr, eval_calls = _gateway_in_turn_goal_env(
        tmp_path, monkeypatch, stream_id=stream_id
    )
    subscriber = channel.subscribe()

    gateway_chat._run_gateway_chat_streaming(
        s.session_id,
        "continue - subagents have finished",
        "test-model",
        str(tmp_path),
        stream_id,
        [],
        goal_related=False,
    )

    events = []
    while not subscriber.empty():
        events.append(subscriber.get_nowait())
    event_names = [item[0] for item in events]
    assert eval_calls == []
    assert fake_mgr.state.turns_used == 1
    assert "goal" in event_names
    # The Agent's own hook dispatched it; a WebUI dispatch would be a second.
    assert "goal_continue" not in event_names
    assert s.session_id not in PENDING_GOAL_CONTINUATION
    goal_messages = [
        str(item[1].get("message") or "") for item in events if item[0] == "goal"
    ]
    assert any("1/100" in memo for memo in goal_messages), goal_messages


def test_gateway_turn_with_native_hook_and_goal_related_false_launches_nothing(
    tmp_path, monkeypatch
):
    """An Agent-owned in-turn decision launches nothing from the WebUI."""
    stream_id = "stream-gateway-native-owned"
    s, channel, fake_mgr, eval_calls = _gateway_in_turn_goal_env(
        tmp_path, monkeypatch, stream_id=stream_id
    )

    launches = []
    monkeypatch.setattr(
        routes,
        "start_session_turn",
        lambda *a, **kw: launches.append(("detached", a, kw)) or {"_status": 200},
    )
    monkeypatch.setattr(
        webui_goals,
        "start_detached_goal_continuation",
        lambda *a, **kw: launches.append(("handoff", a, kw)),
    )

    # No SSE subscriber: the detached branch is the only WebUI dispatcher.
    gateway_chat._run_gateway_chat_streaming(
        s.session_id,
        "continue - subagents have finished",
        "test-model",
        str(tmp_path),
        stream_id,
        [],
        goal_related=False,
    )
    assert launches == []
    assert eval_calls == []
    assert fake_mgr.state.turns_used == 1
    assert s.session_id not in PENDING_GOAL_CONTINUATION
    late = channel.subscribe()
    replayed = []
    while not late.empty():
        replayed.append(late.get_nowait()[0])
    assert "goal_continue" not in replayed


def test_gateway_worker_starts_detached_goal_continuation_when_no_sse_subscriber(
    tmp_path, monkeypatch
):
    """When goal_continue fires on a stream with 0 SSE subscribers, teardown starts the next turn server-side."""
    session_dir = tmp_path / "sessions"
    session_dir.mkdir()
    monkeypatch.setattr(models, "SESSION_DIR", session_dir)
    monkeypatch.setattr(models, "SESSION_INDEX_FILE", session_dir / "_index.json")
    monkeypatch.setattr(models, "SESSIONS", OrderedDict())

    class FakeResponse:
        def __enter__(self):
            return self

        def __exit__(self, exc_type, exc, tb):
            return False

        def __iter__(self):
            yield b'data: {"choices":[{"delta":{"content":"wakeup handled"}}]}\n\n'
            yield b"data: [DONE]\n\n"

    monkeypatch.setenv("HERMES_WEBUI_GATEWAY_BASE_URL", "http://gateway.local")
    monkeypatch.setattr(gateway_chat.urllib.request, "urlopen", lambda req, timeout=0: FakeResponse())
    monkeypatch.setattr(webui_goals, "has_active_goal", lambda *_a, **_kw: True)
    monkeypatch.setattr(
        webui_goals,
        "evaluate_goal_after_turn",
        lambda *_a, **_kw: {
            "status": "active",
            "should_continue": True,
            "continuation_prompt": "[Continuing toward your standing goal]\nGoal: finish wave 4",
            "message": "↻ Continuing toward goal (2/100): spawn wave 4",
            "message_key": "goal_continuing",
            "message_args": [2, 100, "spawn wave 4"],
        },
    )

    import threading as _threading

    detached_done = _threading.Event()
    detached_calls = []

    def fake_start_session_turn(sid, prompt, *, source="process_wakeup", goal_continuation_id="", **_kw):
        detached_calls.append((sid, prompt, source, goal_continuation_id))
        detached_done.set()
        return {"_status": 200, "stream_id": "next-goal-stream", "session_id": sid}

    monkeypatch.setattr(routes, "start_session_turn", fake_start_session_turn)

    s = new_session()
    stream_id = "stream-gateway-detached-goal"
    s.active_stream_id = stream_id
    s.pending_user_message = "[IMPORTANT: A delegated subagent finished.]"
    s.pending_user_source = "process_wakeup"
    s.pending_attachments = []
    s.pending_started_at = 123
    s.save()
    # Channel has NO subscriber attached (closed tab / background session wakeup).
    channel = create_stream_channel()
    STREAMS[stream_id] = channel
    PENDING_GOAL_CONTINUATION.discard(s.session_id)

    gateway_chat._run_gateway_chat_streaming(
        s.session_id,
        "[IMPORTANT: A delegated subagent finished.]",
        "test-model",
        str(tmp_path),
        stream_id,
        [],
        goal_related=True,
    )
    assert detached_done.wait(timeout=3.0), "detached goal continuation did not fire start_session_turn"
    cid = webui_goals.peek_pending_goal_continuation_id(s.session_id)
    assert cid
    assert detached_calls == [
        (s.session_id, "[Continuing toward your standing goal]\nGoal: finish wave 4", "goal_wakeup", cid)
    ]
    # Must fix 2: one dispatcher. The detached path never publishes (or
    # buffers for replay) goal_continue, so a late-attaching browser
    # cannot dispatch the same record a second time.
    late = channel.subscribe()
    replayed = []
    while not late.empty():
        replayed.append(late.get_nowait()[0])
    assert "goal_continue" not in replayed


# ── Review 5578589772: teardown must survive error/cancel exits ─────────────


@pytest.mark.parametrize("exit_kind", ["error", "cancel"])
def test_run_agent_streaming_error_or_cancel_exit_does_not_unbound_continuation(
    monkeypatch, exit_kind
):
    """An exit before the goal hook must not raise UnboundLocalError in teardown."""
    from api import streaming

    sid = f"sid-teardown-{exit_kind}"
    stream_id = f"stream-teardown-{exit_kind}"
    STREAMS[stream_id] = create_stream_channel()
    started = []
    monkeypatch.setattr(
        webui_goals, "start_detached_goal_continuation", lambda *a, **k: started.append(a)
    )

    def _boom(*_a, **_kw):
        if exit_kind == "cancel":
            ev = streaming.CANCEL_FLAGS.get(stream_id)
            if ev is not None:
                ev.set()
            raise InterruptedError("cancelled before goal hook")
        raise RuntimeError("turn failed before goal hook")

    monkeypatch.setattr(streaming, "get_session", _boom)
    try:
        streaming._run_agent_streaming(
            session_id=sid, msg_text="hi", model="m", workspace="/tmp", stream_id=stream_id
        )
    except UnboundLocalError as exc:  # pragma: no cover - the regression
        pytest.fail(f"teardown leaked UnboundLocalError: {exc}")
    finally:
        STREAMS.pop(stream_id, None)
    assert started == []


# ── Detached continuation invariants (cancel / queued turn / profile) ───────


def _detached_env(monkeypatch, session, *, active_turn=False, isolated=False, active_profile="default"):
    from api import background_process, profiles

    webui_goals.clear_pending_goal_continuation(session.session_id)
    session.cid = webui_goals.register_pending_goal_continuation(session.session_id, "continue")
    monkeypatch.setattr(models, "get_session", lambda _sid, *a, **k: session)
    monkeypatch.setattr(background_process, "_session_has_active_turn", lambda _sid: active_turn)
    monkeypatch.setattr(profiles, "_is_isolated_profile_mode", lambda: isolated)
    monkeypatch.setattr(profiles, "get_active_profile_name", lambda: active_profile)
    homes = []
    monkeypatch.setattr(
        profiles, "get_hermes_home_for_profile", lambda name: homes.append(name) or f"/homes/{name}"
    )
    monkeypatch.setattr(webui_goals, "has_active_goal", lambda _sid, **kw: kw.get("profile_home") is not None)
    return homes


def _sess(**kw):
    base = dict(session_id="sid-detached", profile="alice", messages=[], pending_user_message=None)
    base.update(kw)
    return SimpleNamespace(**base)


def test_detached_continuation_allowed_resolves_owning_profile(monkeypatch):
    s = _sess()
    homes = _detached_env(monkeypatch, s)
    assert webui_goals.detached_goal_continuation_skip_reason(s.session_id, s.cid) == ""
    assert homes == ["alice"]
    assert webui_goals.detached_goal_continuation_skip_reason(s.session_id, "0" * 32) == "not_pending"


@pytest.mark.parametrize(
    "case, expected",
    [
        ("not_pending", "not_pending"),
        ("cancel_marker", "cancelled"),
        ("queued_message", "turn_pending"),
        ("active_turn", "turn_active"),
        ("foreign_profile", "foreign_profile"),
        ("goal_inactive", "goal_inactive"),
    ],
)


def test_detached_continuation_skip_reasons(monkeypatch, case, expected):
    s = _sess()
    kw = {}
    if case == "cancel_marker":
        s.messages = [
            {"role": "user", "content": "go"},
            {"role": "assistant", "content": "*Task cancelled.*"},
        ]
    elif case == "queued_message":
        s.pending_user_message = "user typed next"
    elif case == "active_turn":
        kw["active_turn"] = True
    elif case == "foreign_profile":
        kw.update(isolated=True, active_profile="bob")
    _detached_env(monkeypatch, s, **kw)
    if case == "not_pending":
        webui_goals.clear_pending_goal_continuation(s.session_id)
    if case == "goal_inactive":
        monkeypatch.setattr(webui_goals, "has_active_goal", lambda *_a, **_kw: False)
    assert webui_goals.detached_goal_continuation_skip_reason(s.session_id, s.cid) == expected


def test_detached_continuation_on_sealed_parent_starts_no_turn_and_drops_record(monkeypatch):
    import threading

    from api import compression_continuation

    s = _sess()
    _detached_env(monkeypatch, s)
    monkeypatch.setattr(compression_continuation, "durable_compression_continuation", lambda _s: (True, "tip-sid"))
    assert webui_goals.detached_goal_continuation_skip_reason(s.session_id, s.cid) == "sealed_parent"
    started = []
    monkeypatch.setattr(routes, "start_session_turn", lambda *a, **k: started.append(a))
    threads = []
    real_thread = threading.Thread

    def _thread(*a, **k):
        t = real_thread(*a, **k)
        threads.append(t)
        return t

    monkeypatch.setattr(webui_goals.threading, "Thread", _thread)
    webui_goals.start_detached_goal_continuation(s.session_id, "continue", s.cid)
    for t in threads:
        t.join(5)
    assert started == []
    assert not webui_goals.pending_goal_continuation_matches(s.session_id, s.cid)


def test_start_detached_goal_continuation_does_not_start_when_skipped(monkeypatch):
    import threading

    started = []
    monkeypatch.setattr(routes, "start_session_turn", lambda *a, **k: started.append(a))
    monkeypatch.setattr(webui_goals, "detached_goal_continuation_skip_reason", lambda _sid, _cid: "cancelled")
    threads = []
    real_thread = threading.Thread

    def _thread(*a, **k):
        t = real_thread(*a, **k)
        threads.append(t)
        return t

    monkeypatch.setattr(webui_goals.threading, "Thread", _thread)
    cid = "a" * 32
    webui_goals.start_detached_goal_continuation("sid-x", "continue", cid)
    for t in threads:
        t.join(5)
    assert started == []

    monkeypatch.setattr(routes, "start_session_turn", lambda *a, **k: started.append((a, k)))
    monkeypatch.setattr(webui_goals, "detached_goal_continuation_skip_reason", lambda _sid, _cid: "")
    webui_goals.start_detached_goal_continuation("sid-x", "continue", cid)
    for t in threads:
        t.join(5)
    assert started == [(("sid-x", "continue"), {"source": "goal_wakeup", "goal_continuation_id": cid})]


# ── Review 5593825504: regression table ─────────────────────────────────────


class _EvalState:
    goal = "ship the feature"
    status = "active"
    turns_used = 2
    max_turns = 20
    last_verdict = "continue"
    last_reason = "more work"
    paused_reason = None
    gates: list = []


class _EvalMgr:
    def __init__(self, state, *, waiting=False):
        self.state = state
        self._waiting = waiting

    def is_active(self):
        return self.state is not None and self.state.status == "active"

    def is_waiting(self):
        return self._waiting

    def _waiting_decision(self, _state):
        return {"status": "active", "should_continue": False, "continuation_prompt": None,
                "verdict": "waiting", "reason": "r", "message": "⏳ parked"}

    def next_continuation_prompt(self):
        return f"[Continuing toward your standing goal]\nGoal: {self.state.goal}" if self.is_active() else None

    def pause(self, reason="user-paused"):
        self.state.status = "paused"
        self.state.paused_reason = reason
        return self.state


@pytest.mark.parametrize("verdict", ["wait", "continue"])
def test_in_turn_adoption_pauses_a_goal_past_its_turn_budget(verdict):
    """A native WAIT parks without a budget check, so the adopted decision must pause at max_turns."""
    st = _EvalState()
    st.turns_used, st.max_turns, st.last_verdict = 156, 100, verdict
    decision = webui_goals._decision_from_evaluated_state(_EvalMgr(st, waiting=False), st)
    assert decision is not None
    assert decision["should_continue"] is False
    assert decision["continuation_prompt"] is None
    assert decision["status"] == "paused"
    assert st.status == "paused"
    assert st.paused_reason == "turn budget exhausted (156/100)"
    under = _EvalState()
    under.turns_used, under.max_turns, under.last_verdict = 99, 100, verdict
    under_decision = webui_goals._decision_from_evaluated_state(_EvalMgr(under), under)
    assert under_decision is not None and under_decision["should_continue"] is True


@pytest.mark.parametrize("status", ["cleared", "", "bogus"])
def test_in_turn_adoption_never_continues_a_cleared_or_unknown_goal(status):
    """A cleared or unknown goal status never continues the loop."""
    st = _EvalState()
    st.status = status
    decision = webui_goals._decision_from_evaluated_state(_EvalMgr(st), st)
    assert decision is not None
    assert decision["should_continue"] is False
    assert decision["continuation_prompt"] is None


def test_in_turn_adoption_uses_live_wait_state_not_stale_wait_verdict():
    """A lifted wait barrier uses the live state, not the stale wait verdict."""
    st = _EvalState()
    st.last_verdict = "wait"
    decision = webui_goals._decision_from_evaluated_state(_EvalMgr(st, waiting=False), st)
    assert decision["should_continue"] is True
    parked = webui_goals._decision_from_evaluated_state(_EvalMgr(st, waiting=True), st)
    assert parked["should_continue"] is False


def test_in_turn_adoption_rebuilds_failed_quality_gate_after_resume(monkeypatch):
    """A gate failure is rebuilt into a continuation after resume."""
    monkeypatch.setattr(
        webui_goals,
        "CONTINUATION_PROMPT_GATE_FAILED_TEMPLATE",
        "GATE {goal} $ {command} exit {exit_code} {attempt}/{max_retries}\n{output}",
    )
    st = _EvalState()
    st.last_verdict = "wait"
    st.gates = [
        SimpleNamespace(command="true", attempts=0, last_exit_code=0, max_retries=5, last_output_tail=""),
        SimpleNamespace(command="pytest -q", attempts=2, last_exit_code=1, max_retries=5,
                        last_output_tail="1 failed"),
    ]
    decision = webui_goals._decision_from_evaluated_state(_EvalMgr(st), st)
    assert decision["verdict"] == "gate_failed"
    assert decision["should_continue"] is True
    assert decision["continuation_prompt"] == "GATE ship the feature $ pytest -q exit 1 2/5\n1 failed"
    assert "attempt 2/5" in decision["message"]


def test_in_turn_adoption_declines_gate_failure_it_cannot_rebuild(monkeypatch):
    monkeypatch.setattr(webui_goals, "CONTINUATION_PROMPT_GATE_FAILED_TEMPLATE", "")
    st = _EvalState()
    st.gates = [SimpleNamespace(command="x", attempts=1, last_exit_code=2, max_retries=5, last_output_tail="")]
    assert webui_goals._decision_from_evaluated_state(_EvalMgr(st), st) is None


@pytest.fixture
def _admission_env(tmp_path, monkeypatch):
    import api.config as config

    session_dir = tmp_path / "sessions"
    session_dir.mkdir()
    monkeypatch.setattr(models, "SESSION_DIR", session_dir)
    monkeypatch.setattr(models, "SESSION_INDEX_FILE", session_dir / "_index.json")
    monkeypatch.setattr(config, "SESSION_INDEX_FILE", session_dir / "_index.json", raising=False)
    monkeypatch.setattr(models, "SESSIONS", OrderedDict())
    monkeypatch.setattr(routes, "set_last_workspace", lambda *_a, **_kw: None)
    monkeypatch.setattr(routes, "publish_session_list_changed", lambda *_a, **_kw: None)
    monkeypatch.setattr(routes, "_agent_runtime_barrier_response", lambda **_kw: None)
    monkeypatch.setattr(routes, "_active_run_stream_for_session", lambda _sid: None)
    monkeypatch.setattr(routes, "_run_agent_streaming", lambda *_a, **_kw: None)
    monkeypatch.setattr(routes, "_run_gateway_chat_streaming", lambda *_a, **_kw: None)
    goal_active = {"value": True}
    monkeypatch.setattr(webui_goals, "has_active_goal", lambda *_a, **_kw: goal_active["value"])
    s = new_session(workspace=str(tmp_path), profile="default")
    webui_goals.clear_pending_goal_continuation(s.session_id)
    yield s, goal_active
    webui_goals.clear_pending_goal_continuation(s.session_id)
    if s.active_stream_id:
        STREAMS.pop(s.active_stream_id, None)
        config.STREAM_GOAL_RELATED.pop(s.active_stream_id, None)


def _admit(s, tmp_path, cid, source="goal_wakeup"):
    return routes._start_chat_stream_for_session(
        s, msg="continue the goal", attachments=[], workspace=str(tmp_path),
        model=s.model, model_provider=s.model_provider, external_runtime_owned=False,
        source=source, goal_continuation_id=cid,
    )


def test_one_record_admits_exactly_one_goal_related_launch(_admission_env, tmp_path):
    """One record admits exactly one goal-related launch."""
    import api.config as config

    s, _ = _admission_env
    cid = webui_goals.register_pending_goal_continuation(s.session_id, "continue the goal")
    resp = _admit(s, tmp_path, cid)
    assert resp.get("_status", 200) == 200, resp
    assert config.STREAM_GOAL_RELATED.get(resp["stream_id"]) is True
    assert webui_goals.peek_pending_goal_continuation_id(s.session_id) is None
    assert webui_goals.consume_pending_goal_continuation(s.session_id, cid) is False


@pytest.mark.parametrize("change", ["pause", "clear"])
def test_goal_wakeup_after_pause_or_clear_is_rejected_not_downgraded(_admission_env, tmp_path, change):
    """A pause or clear after the pre-flight check rejects the wakeup with 409, never downgrading it."""
    s, goal_active = _admission_env
    cid = webui_goals.register_pending_goal_continuation(s.session_id, "continue the goal")
    goal_active["value"] = False
    if change == "clear":
        webui_goals.clear_pending_goal_continuation(s.session_id)
    resp = _admit(s, tmp_path, cid)
    assert resp["_status"] == 409
    assert resp["error"] == "goal_continuation_rejected"
    assert not getattr(s, "active_stream_id", None)
    if change == "pause":
        # Pause keeps the record for the user's /goal resume (#7855).
        assert webui_goals.peek_pending_goal_continuation_id(s.session_id) == cid


def test_goal_wakeup_without_its_record_id_is_rejected(_admission_env, tmp_path):
    s, _ = _admission_env
    webui_goals.register_pending_goal_continuation(s.session_id, "continue the goal")
    resp = _admit(s, tmp_path, "b" * 32)
    assert resp["_status"] == 409
    assert webui_goals.peek_pending_goal_continuation_id(s.session_id)


def test_rejected_goal_wakeup_start_restores_record(_admission_env, tmp_path, monkeypatch):
    """A start rejected after admission restores the record via master's receipt."""
    s, _ = _admission_env
    cid = webui_goals.register_pending_goal_continuation(s.session_id, "continue the goal")
    monkeypatch.setattr(
        routes, "create_stream_channel", lambda: (_ for _ in ()).throw(RuntimeError("rejected"))
    )
    with pytest.raises(RuntimeError):
        _admit(s, tmp_path, cid)
    assert webui_goals.peek_pending_goal_continuation_id(s.session_id) == cid


@pytest.mark.parametrize("action", ["pause", "clear"])
def test_goal_pause_and_clear_run_under_the_session_lock(monkeypatch, tmp_path, action):
    """Must fix 3: pause/clear serialize with goal_wakeup admission (same lock)."""
    from api.config import _get_session_agent_lock

    session, _holder, _k = _goal_command_env(monkeypatch, tmp_path, f"sid-lock-{action}")
    held = []
    real = webui_goals.goal_command_payload

    def spy(sid, text, **kw):
        held.append(_get_session_agent_lock(sid).locked())
        return real(sid, text, **kw)

    monkeypatch.setattr(webui_goals, "goal_command_payload", spy)
    assert routes._handle_goal_command(object(), {"session_id": session.session_id, "args": action})["status"] == 200
    assert held == [True]


# ── Review 5603344218 Must fix 2: a detached record keeps its dispatcher ────


def _capture_goal_threads(monkeypatch):
    """Capture (and join) the detached runner threads spawned by api.goals."""
    import threading as _threading

    threads = []
    real_thread = _threading.Thread

    def _thread(*a, **k):
        t = real_thread(*a, **k)
        threads.append(t)
        return t

    monkeypatch.setattr(webui_goals.threading, "Thread", _thread)

    def join_all(timeout=5.0):
        for t in list(threads):
            t.join(timeout)

    return join_all


def test_pause_after_detached_preflight_then_resume_dispatches_once(
    monkeypatch, tmp_path
):
    """A pause between preflight and admission, then resume, dispatches exactly once."""
    session, holder, kickoffs = _goal_command_env(
        monkeypatch, tmp_path, "sid-detached-paused-resume"
    )
    webui_goals.clear_pending_goal_continuation(session.session_id)
    cid = webui_goals.register_pending_goal_continuation(
        session.session_id, "continue the goal"
    )
    assert webui_goals.mark_detached_goal_continuation(session.session_id, cid)

    admitted = []

    def fake_start_session_turn(
        sid, prompt, *, source="process_wakeup", goal_continuation_id="", **_kw
    ):
        """Consume the record by ID, or reject while the goal is paused."""
        if holder["state"].status != "active":
            return {
                "_status": 409,
                "error": "goal_continuation_rejected",
                "reason": "goal_inactive",
            }
        receipt = webui_goals.consume_pending_goal_continuation(sid, goal_continuation_id)
        if not receipt:
            return {
                "_status": 409,
                "error": "goal_continuation_rejected",
                "reason": "goal_continuation_stale",
            }
        admitted.append((sid, prompt, source, goal_continuation_id))
        return {"_status": 200, "stream_id": "goal-wakeup-stream", "session_id": sid}

    monkeypatch.setattr(routes, "start_session_turn", fake_start_session_turn)
    # Real preflight with session/model/profile seams stubbed.
    from api import background_process, profiles

    monkeypatch.setattr(models, "get_session", lambda _sid, *a, **k: session)
    monkeypatch.setattr(background_process, "_session_has_active_turn", lambda _sid: False)
    monkeypatch.setattr(profiles, "get_hermes_home_for_profile", lambda _name: str(tmp_path))
    monkeypatch.setattr(
        webui_goals,
        "has_active_goal",
        lambda _sid, **_kw: holder["state"].status == "active",
    )
    join_all = _capture_goal_threads(monkeypatch)
    try:
        # The pause lands after preflight, before admission: the wakeup is rejected.
        assert (
            routes._handle_goal_command(
                object(), {"session_id": session.session_id, "args": "pause"}
            )["status"]
            == 200
        )
        assert webui_goals.peek_pending_goal_continuation_id(session.session_id) == cid

        res = routes._handle_goal_command(
            object(), {"session_id": session.session_id, "args": "resume"}
        )
        assert res["status"] == 200
        join_all()
        assert kickoffs == [], "resume must retry the record, not start a second dispatcher"
        assert admitted == [
            (session.session_id, "continue the goal", "goal_wakeup", cid)
        ], "exactly one recoverable goal-related dispatch"
        assert webui_goals.peek_pending_goal_continuation_id(session.session_id) is None
    finally:
        join_all()
        webui_goals.clear_pending_goal_continuation(session.session_id)


@pytest.mark.parametrize("detached_owned, expected", [(True, 1), (False, 0)])
def test_gateway_teardown_retries_only_webui_owned_records(
    tmp_path, monkeypatch, detached_owned, expected
):
    """Teardown retries only WebUI-owned records, never browser-held ones."""
    stream_id = f"stream-gateway-idle-retry-{detached_owned}"
    s, channel, _fake_mgr, _eval_calls = _gateway_in_turn_goal_env(
        tmp_path, monkeypatch, stream_id=stream_id
    )
    monkeypatch.setattr(webui_goals, "has_active_goal", lambda *_a, **_kw: False)
    cid = webui_goals.register_pending_goal_continuation(
        s.session_id, "leftover continuation"
    )
    if detached_owned:
        assert webui_goals.mark_detached_goal_continuation(s.session_id, cid)

    retried = []
    monkeypatch.setattr(
        webui_goals,
        "start_detached_goal_continuation",
        lambda sid, prompt, cont_id: retried.append((sid, prompt, cont_id)),
    )
    gateway_chat._run_gateway_chat_streaming(
        s.session_id,
        "continue - subagents have finished",
        "test-model",
        str(tmp_path),
        stream_id,
        [],
        goal_related=True,
    )
    assert len(retried) == expected
    if expected:
        assert retried == [(s.session_id, "leftover continuation", cid)]


def test_retry_detached_goal_continuation_binds_ownership_to_the_id(monkeypatch):
    """Ownership is bound to the ID, and retries reuse it so admission stays the single arbiter."""
    sid = "sid-detached-ownership"
    webui_goals.clear_pending_goal_continuation(sid)
    started = []
    monkeypatch.setattr(
        webui_goals,
        "start_detached_goal_continuation",
        lambda s, prompt, cont_id: started.append((s, prompt, cont_id)),
    )
    cid = webui_goals.register_pending_goal_continuation(sid, "continue the goal")
    # Browser-owned: the browser holds that dispatch, so nothing is retried.
    assert webui_goals.retry_detached_goal_continuation(sid) is False
    assert started == []
    # A mark for another ID (consumed/replaced record) is ignored.
    assert webui_goals.mark_detached_goal_continuation(sid, "0" * 32) is False
    assert webui_goals.retry_detached_goal_continuation(sid) is False
    assert started == []
    # WebUI-owned: retried with its EXISTING ID.
    assert webui_goals.mark_detached_goal_continuation(sid, cid) is True
    assert webui_goals.retry_detached_goal_continuation(sid) is True
    assert started == [(sid, "continue the goal", cid)]
    webui_goals.clear_pending_goal_continuation(sid)
    assert webui_goals.retry_detached_goal_continuation(sid) is False


# ── Browser hand-off is a lease: the server dispatches at the deadline ─────

_LEASE_DECISION = {
    "status": "active",
    "should_continue": True,
    "continuation_prompt": "[Continuing toward your standing goal]\nGoal: remove helpers",
    "message": "Continuing toward goal (73/100)",
    "message_key": "goal_continuing",
    "message_args": [],
}


def _browser_lease_env(tmp_path, monkeypatch, *, stream_id, grace):
    """Gateway turn whose goal_continue is handed to a subscriber that never posts it back."""
    import threading as _threading

    _isolated_sessions(tmp_path, monkeypatch)
    _gateway_response(monkeypatch)
    monkeypatch.setattr(
        webui_goals, "GOAL_CONTINUATION_BROWSER_GRACE_SECONDS", grace, raising=False
    )
    monkeypatch.setattr(webui_goals, "has_active_goal", lambda *_a, **_kw: True)
    monkeypatch.setattr(
        webui_goals, "evaluate_goal_after_turn", lambda *_a, **_kw: dict(_LEASE_DECISION)
    )
    from api import background_process, profiles

    monkeypatch.setattr(
        background_process, "_session_has_active_turn", lambda _sid: False
    )
    monkeypatch.setattr(
        profiles, "get_hermes_home_for_profile", lambda _name: str(tmp_path)
    )

    admitted = []
    started = _threading.Event()

    def fake_start_session_turn(
        sid, prompt, *, source="process_wakeup", goal_continuation_id="", **_kw
    ):
        """Real admission contract: consume the record by ID or reject."""
        receipt = webui_goals.consume_pending_goal_continuation(
            sid, goal_continuation_id
        )
        if not receipt:
            return {
                "_status": 409,
                "error": "goal_continuation_rejected",
                "reason": "goal_continuation_stale",
            }
        admitted.append((sid, prompt, source, goal_continuation_id))
        started.set()
        return {"_status": 200, "stream_id": "lease-fallback-stream", "session_id": sid}

    monkeypatch.setattr(routes, "start_session_turn", fake_start_session_turn)

    s = new_session()
    s.active_stream_id = stream_id
    s.pending_user_message = "[Continuing toward your standing goal]"
    s.pending_user_source = "goal_wakeup"
    s.pending_attachments = []
    s.pending_started_at = 123
    s.save()
    channel = create_stream_channel()
    STREAMS[stream_id] = channel
    PENDING_GOAL_CONTINUATION.discard(s.session_id)
    return s, channel, admitted, started


def _run_lease_turn(s, channel, tmp_path, stream_id):
    sub = channel.subscribe()
    gateway_chat._run_gateway_chat_streaming(
        s.session_id,
        "[Continuing toward your standing goal]",
        "test-model",
        str(tmp_path),
        stream_id,
        [],
        goal_related=True,
    )
    events = []
    while not sub.empty():
        events.append(sub.get_nowait()[0])
    return events


def test_browser_owned_continuation_falls_back_after_grace(tmp_path, monkeypatch):
    """A subscriber that never posts the continuation back must not strand the goal."""
    stream_id = "stream-lease-fallback"
    s, channel, admitted, started = _browser_lease_env(
        tmp_path, monkeypatch, stream_id=stream_id, grace=0.2
    )
    events = _run_lease_turn(s, channel, tmp_path, stream_id)
    assert "goal_continue" in events
    cid = webui_goals.peek_pending_goal_continuation_id(s.session_id)
    assert cid
    assert (
        webui_goals.pending_detached_goal_continuation(s.session_id) is None
    ), "the browser holds the first attempt"
    assert started.wait(timeout=5.0), (
        "browser-owned continuation stranded — no turn started after the grace "
        f"(pending id={webui_goals.peek_pending_goal_continuation_id(s.session_id)!r})"
    )
    assert admitted == [
        (s.session_id, _LEASE_DECISION["continuation_prompt"], "goal_wakeup", cid)
    ]
    assert webui_goals.peek_pending_goal_continuation_id(s.session_id) is None


def test_browser_dispatch_inside_grace_disarms_the_server_fallback(
    tmp_path, monkeypatch
):
    """A browser POST inside the grace disarms the server fallback."""
    import time as _time

    stream_id = "stream-lease-browser-wins"
    grace = 0.4
    s, channel, admitted, started = _browser_lease_env(
        tmp_path, monkeypatch, stream_id=stream_id, grace=grace
    )
    events = _run_lease_turn(s, channel, tmp_path, stream_id)
    assert "goal_continue" in events
    cid = webui_goals.peek_pending_goal_continuation_id(s.session_id)
    assert webui_goals.consume_pending_goal_continuation(s.session_id, cid)
    _time.sleep(grace + 0.4)
    assert admitted == []
    assert not started.is_set()


@pytest.mark.parametrize("change", ["pause", "clear"])
def test_goal_pause_or_clear_during_grace_never_dispatches(
    tmp_path, monkeypatch, change
):
    """A pause or clear before the deadline never dispatches."""
    import time as _time

    stream_id = f"stream-lease-{change}"
    grace = 0.4
    s, channel, admitted, started = _browser_lease_env(
        tmp_path, monkeypatch, stream_id=stream_id, grace=grace
    )
    assert "goal_continue" in _run_lease_turn(s, channel, tmp_path, stream_id)
    if change == "clear":
        webui_goals.clear_pending_goal_continuation(s.session_id)
    else:
        monkeypatch.setattr(webui_goals, "has_active_goal", lambda *_a, **_kw: False)
    _time.sleep(grace + 0.4)
    assert admitted == []
    assert not started.is_set()


def test_browser_lease_expires_into_one_dispatch_bound_to_its_id(monkeypatch):
    """Nothing dispatches before the deadline; exactly one dispatch after, bound to the record's ID."""
    import time as _time

    from api.config import (
        PENDING_GOAL_CONTINUATION_LOCK,
        PENDING_GOAL_CONTINUATION_PROMPTS,
    )

    sid = "sid-browser-lease-unit"
    webui_goals.clear_pending_goal_continuation(sid)
    started = []
    monkeypatch.setattr(
        webui_goals,
        "start_detached_goal_continuation",
        lambda s, prompt, cont_id: started.append((s, prompt, cont_id)),
    )
    cid = webui_goals.register_pending_goal_continuation(sid, "continue the goal")
    assert webui_goals.hand_goal_continuation_to_browser(
        sid, cid, grace_seconds=30
    ) is True
    assert webui_goals.dispatch_browser_owned_goal_continuation(sid, cid) is False
    assert webui_goals.retry_detached_goal_continuation(sid) is False
    assert started == []
    # The deadline passes with the record still pending: the tab never sent it.
    with PENDING_GOAL_CONTINUATION_LOCK:
        PENDING_GOAL_CONTINUATION_PROMPTS[sid]["browser_grace_deadline"] = (
            _time.monotonic() - 1
        )
    assert webui_goals.dispatch_browser_owned_goal_continuation(sid, "0" * 32) is False
    assert webui_goals.retry_detached_goal_continuation(sid) is True
    assert started == [(sid, "continue the goal", cid)]
    assert webui_goals.pending_goal_continuation_record(sid)["detached_owned"] is True
    assert webui_goals.consume_pending_goal_continuation(sid, cid)
    assert webui_goals.retry_detached_goal_continuation(sid) is False


def test_late_browser_post_after_server_fallback_is_not_a_second_turn(
    _admission_env, tmp_path
):
    """A POST with an already-consumed ID is a duplicate, not a second turn."""
    s, _goal_active = _admission_env
    cid = webui_goals.register_pending_goal_continuation(
        s.session_id, "continue the goal"
    )
    # The fallback ran first and consumed the record.
    assert webui_goals.consume_pending_goal_continuation(s.session_id, cid)
    resp = _admit(s, tmp_path, cid, source="webui")
    assert resp["_status"] == 409
    assert resp["error"] == "goal_continuation_already_dispatched"
    assert not getattr(s, "active_stream_id", None)
    # A genuine user turn (no ID) is untouched by the dedupe.
    monkeypatch_resp = _admit(s, tmp_path, "", source="webui")
    assert monkeypatch_resp.get("_status", 200) == 200, monkeypatch_resp


def test_native_agent_owned_gateway_turn_arms_no_browser_lease(
    tmp_path, monkeypatch
):
    """An Agent-owned Gateway turn arms no browser lease."""
    stream_id = "stream-lease-native-owned"
    s, channel, _fake_mgr, _eval_calls = _gateway_in_turn_goal_env(
        tmp_path, monkeypatch, stream_id=stream_id
    )
    sub = channel.subscribe()
    launches = []
    monkeypatch.setattr(
        routes,
        "start_session_turn",
        lambda *a, **kw: launches.append(("detached", a)) or {"_status": 200},
    )
    monkeypatch.setattr(
        webui_goals,
        "start_detached_goal_continuation",
        lambda *a, **kw: launches.append(("handoff", a)),
    )
    gateway_chat._run_gateway_chat_streaming(
        s.session_id,
        "continue - subagents have finished",
        "test-model",
        str(tmp_path),
        stream_id,
        [],
        goal_related=False,
    )
    events = []
    while not sub.empty():
        events.append(sub.get_nowait()[0])
    assert "goal" in events
    assert "goal_continue" not in events
    assert launches == []
    assert s.session_id not in PENDING_GOAL_CONTINUATION
    assert s.session_id not in getattr(webui_goals, "_BROWSER_GRACE_TIMERS", {})


def test_both_dispatch_sites_hand_the_continuation_over_with_a_lease():
    """Gateway and local streaming both reach the browser lease through one shared helper."""
    from pathlib import Path

    for name in ("api/gateway_chat.py", "api/streaming.py"):
        src = Path(name).read_text()
        assert "dispatch_goal_continuation(" in src, name
        assert "hand_goal_continuation_to_browser(" not in src, name
        assert "mark_detached_goal_continuation(" not in src, name


# ── Review 5639825728: the record is the authority, the tombstone a label ───


def _wait_for(predicate, timeout=5.0, interval=0.02):
    """Bounded wait for a background dispatch (no unbounded sleeps)."""
    import time as _time

    deadline = _time.monotonic() + timeout
    while _time.monotonic() < deadline:
        if predicate():
            return True
        _time.sleep(interval)
    return predicate()


def _consume_unrelated_continuations(count: int = 64) -> None:
    """Consume *count* unrelated records, evicting the bounded spent-ID cache."""
    for i in range(count):
        sid = f"sid-review-5639825728-churn-{i}"
        cid = webui_goals.register_pending_goal_continuation(sid, "continue the goal")
        assert cid
        assert webui_goals.consume_pending_goal_continuation(sid, cid)


@pytest.fixture
def _lease_route_env(_admission_env, tmp_path, monkeypatch):
    """Real route admission plus a real Gateway completion; only the worker body is stubbed."""
    import threading as _threading

    from api import background_process, profiles

    s, goal_active = _admission_env
    monkeypatch.setattr(
        webui_goals, "GOAL_CONTINUATION_BROWSER_GRACE_SECONDS", 0.2, raising=False
    )
    monkeypatch.setattr(
        webui_goals, "evaluate_goal_after_turn", lambda *_a, **_kw: dict(_LEASE_DECISION)
    )
    monkeypatch.setattr(
        background_process, "_session_has_active_turn", lambda _sid: False
    )
    monkeypatch.setattr(
        profiles, "get_hermes_home_for_profile", lambda _name: str(tmp_path)
    )
    _gateway_response(monkeypatch)

    launches = []
    launch_event = _threading.Event()

    def _worker(session_id, *_a, **kw):
        # The real worker owns the stream until the turn ends; retire the claim
        # so the session is idle again for whatever POST lands next.
        session = models.get_session(session_id)
        session.active_stream_id = None
        session.pending_user_message = None
        session.pending_attachments = None
        session.pending_started_at = None
        session.pending_user_source = None
        session.save()
        launches.append(kw.get("goal_related"))
        launch_event.set()

    monkeypatch.setattr(routes, "_run_agent_streaming", _worker)
    monkeypatch.setattr(routes, "_run_gateway_chat_streaming", _worker)

    def fake_start_session_turn(
        sid, prompt, *, source="process_wakeup", goal_continuation_id="", **_kw
    ):
        """Server dispatch seam: the same session-locked admission route."""
        session = models.get_session(sid)
        return routes._start_chat_stream_for_session(
            session,
            msg=prompt,
            attachments=[],
            workspace=str(tmp_path),
            model=session.model,
            model_provider=session.model_provider,
            external_runtime_owned=False,
            source=source,
            goal_continuation_id=goal_continuation_id,
        )

    monkeypatch.setattr(routes, "start_session_turn", fake_start_session_turn)
    return s, goal_active, launches, launch_event


def _start_gateway_continuation_turn(s, channel, tmp_path, stream_id):
    """Run one Gateway turn that hands its continuation to the subscriber."""
    s.active_stream_id = stream_id
    s.pending_user_message = _LEASE_DECISION["continuation_prompt"]
    s.pending_user_source = "goal_wakeup"
    s.pending_attachments = []
    s.pending_started_at = 123
    s.save()
    sub = channel.subscribe()
    gateway_chat._run_gateway_chat_streaming(
        s.session_id,
        _LEASE_DECISION["continuation_prompt"],
        "test-model",
        str(tmp_path),
        stream_id,
        [],
        goal_related=True,
    )
    events = []
    while not sub.empty():
        events.append(sub.get_nowait()[0])
    return events


def test_stale_browser_dispatch_after_fallback_and_churn_starts_no_turn(
    _lease_route_env, tmp_path
):
    """After fallback and churn past the tombstone bound, a stale POST starts nothing."""
    import api.config as config

    s, _goal_active, launches, launch_event = _lease_route_env
    stream_id = "stream-review-5639825728-churn"
    channel = create_stream_channel()
    STREAMS[stream_id] = channel
    extra_streams = []
    try:
        events = _start_gateway_continuation_turn(s, channel, tmp_path, stream_id)
        assert "goal_continue" in events
        cid = webui_goals.peek_pending_goal_continuation_id(s.session_id)
        assert cid, "the Gateway turn handed no continuation to the browser"
        # The tab never posts it back: after the grace the server takes over.
        assert launch_event.wait(timeout=5.0), "the grace fallback never dispatched"
        assert launches == [True], launches
        assert webui_goals.peek_pending_goal_continuation_id(s.session_id) is None

        # Churn beyond the tombstone bound: the ID is no longer remembered.
        _consume_unrelated_continuations(64)
        assert not webui_goals.goal_continuation_already_dispatched(s.session_id, cid)

        # The browser's queued entry finally drains, carrying the spent token.
        resp = routes._start_chat_stream_for_session(
            s,
            msg=_LEASE_DECISION["continuation_prompt"],
            attachments=[],
            workspace=str(tmp_path),
            model=s.model,
            model_provider=s.model_provider,
            external_runtime_owned=False,
            source="webui",
            goal_continuation_id=cid,
        )
        assert resp["_status"] == 409, resp
        assert resp["error"] == "goal_continuation_stale"
        assert launches == [True], "the stale token started a second turn"

        # The genuine no-ID control in the same session is still admitted.
        control = routes._start_chat_stream_for_session(
            s,
            msg="a brand new question",
            attachments=[],
            workspace=str(tmp_path),
            model=s.model,
            model_provider=s.model_provider,
            external_runtime_owned=False,
            source="webui",
            goal_continuation_id="",
        )
        assert control.get("_status", 200) == 200, control
        extra_streams.append(control.get("stream_id"))
    finally:
        PENDING_GOAL_CONTINUATION.discard(s.session_id)
        webui_goals.clear_pending_goal_continuation(s.session_id)
        for sid in [stream_id, *extra_streams]:
            if sid:
                STREAMS.pop(sid, None)
                config.STREAM_GOAL_RELATED.pop(sid, None)


def test_launch_failure_rollback_rearms_the_browser_grace_fallback(
    _lease_route_env, tmp_path, monkeypatch
):
    """A launch failure restores the browser-grace fallback for the same ID."""
    s, _goal_active, launches, launch_event = _lease_route_env
    prompt = _LEASE_DECISION["continuation_prompt"]
    cid = webui_goals.register_pending_goal_continuation(s.session_id, prompt)
    assert webui_goals.hand_goal_continuation_to_browser(
        s.session_id, cid, grace_seconds=0.2
    )
    fail = {"value": True}
    real_create_stream_channel = routes.create_stream_channel

    def flaky_create_stream_channel():
        if fail["value"]:
            raise RuntimeError("launch failed")
        return real_create_stream_channel()

    monkeypatch.setattr(routes, "create_stream_channel", flaky_create_stream_channel)
    # The browser's POST is admitted, then the launch fails and rolls back.
    with pytest.raises(RuntimeError):
        routes._start_chat_stream_for_session(
            s,
            msg=prompt,
            attachments=[],
            workspace=str(tmp_path),
            model=s.model,
            model_provider=s.model_provider,
            external_runtime_owned=False,
            source="webui",
            goal_continuation_id=cid,
        )
    fail["value"] = False
    assert webui_goals.peek_pending_goal_continuation_id(s.session_id) == cid
    assert launch_event.wait(timeout=5.0), (
        "the rolled-back record lost its dispatch obligation"
    )
    assert launches == [True], launches
    assert webui_goals.peek_pending_goal_continuation_id(s.session_id) is None


def test_restore_rearms_only_the_record_it_restores(monkeypatch):
    """A restored browser-held record is re-armed; consume, clear and replacement arm nothing."""
    sid = "sid-review-5639825728-restore"
    webui_goals.clear_pending_goal_continuation(sid)
    started = []
    monkeypatch.setattr(
        webui_goals,
        "start_detached_goal_continuation",
        lambda s, prompt, cont_id: started.append((s, prompt, cont_id)),
    )
    cid = webui_goals.register_pending_goal_continuation(sid, "continue the goal")
    assert webui_goals.hand_goal_continuation_to_browser(sid, cid, grace_seconds=30)
    receipt = webui_goals.consume_pending_goal_continuation(sid, cid)
    assert isinstance(receipt, dict)
    assert sid not in webui_goals._BROWSER_GRACE_TIMERS
    # Successful consume negative: there is no receipt to restore.
    assert webui_goals.restore_pending_goal_continuation(sid, False) is False
    assert sid not in webui_goals._BROWSER_GRACE_TIMERS
    # Rollback: the record's own wakeup comes back with it.
    assert webui_goals.restore_pending_goal_continuation(sid, receipt) is True
    assert sid in webui_goals._BROWSER_GRACE_TIMERS
    # Clear negative: retiring the record retires its wakeup.
    webui_goals.clear_pending_goal_continuation(sid)
    assert sid not in webui_goals._BROWSER_GRACE_TIMERS
    # Replacement negative: a newer record wins, the stale receipt is inert.
    newer = webui_goals.register_pending_goal_continuation(sid, "continue again")
    assert webui_goals.restore_pending_goal_continuation(sid, receipt) is False
    assert sid not in webui_goals._BROWSER_GRACE_TIMERS
    assert webui_goals.peek_pending_goal_continuation_id(sid) == newer
    assert started == []


@pytest.fixture
def _restore_env(monkeypatch):
    """Stub dispatch capture plus a short grace for the rollback tests."""
    monkeypatch.setattr(
        webui_goals, "GOAL_CONTINUATION_BROWSER_GRACE_SECONDS", 0.2, raising=False
    )
    started = []
    monkeypatch.setattr(
        webui_goals,
        "start_detached_goal_continuation",
        lambda s, prompt, cont_id: started.append((s, prompt, cont_id)),
    )
    yield started


@pytest.mark.parametrize("mode", ["expired", "detached", "no-lease"])
def test_restore_rearms_the_dispatch_it_restores(_restore_env, mode):
    """Every rollback shape restores one automatic retry of the SAME id."""
    import time as _time

    started = _restore_env
    sid = f"sid-review-5639825728-restore-{mode}"
    cid = webui_goals.register_pending_goal_continuation(sid, "continue the goal")
    assert cid
    if mode == "no-lease":
        assert webui_goals.mark_detached_goal_continuation(sid, cid)
    else:
        # A live lease: consuming it cancels the timer, so the dispatch
        # below can only come from the rollback.
        assert webui_goals.hand_goal_continuation_to_browser(
            sid, cid, grace_seconds=0.2
        )
        assert sid in webui_goals._BROWSER_GRACE_TIMERS
    receipt = webui_goals.consume_pending_goal_continuation(sid, cid)
    assert isinstance(receipt, dict)
    assert sid not in webui_goals._BROWSER_GRACE_TIMERS
    if mode == "expired":
        # The browser's POST arrived after the deadline: no extra grace.
        receipt["browser_grace_deadline"] = _time.monotonic() - 1
    elif mode == "detached":
        # The grace fallback had marked it WebUI-owned before it failed.
        receipt["detached_owned"] = True
    else:
        assert "browser_grace_deadline" not in receipt
    assert webui_goals.restore_pending_goal_continuation(sid, receipt) is True
    if mode != "expired":
        assert sid in webui_goals._BROWSER_GRACE_TIMERS
    assert _wait_for(lambda: bool(started), timeout=5.0), mode
    assert started == [(sid, "continue the goal", cid)]


@pytest.mark.parametrize("label", ["goal_continuation_stale", "goal_continuation_already_dispatched"])
def test_refused_continuation_keeps_edited_text_and_files(label):
    """Both 409 labels restore edited text and attachments, drop the token, and resend nothing."""
    import json, shutil, subprocess
    from pathlib import Path

    if not shutil.which("node"):
        pytest.skip("node not available")
    src = Path(__file__).parents[1].joinpath("static", "messages.js").read_text(encoding="utf-8")
    start = src.index("    // Refused continuation token:")
    body = src[start : src.index("      return;\n    }", start)] + "      return;\n    }"
    script = """
const calls=[];const INFLIGHT={s1:1};const activeSid='s1';
const e={status:409};const errMsg=%s;
const _failedSendDraftText='continue\\nplus my edits';const _failedSendFilesSnapshot=[{name:'user-notes.txt'}];
const _composerDraftClearPromise=Promise.resolve();
function clearInflightState(){calls.push('clearInflight')}function removeThinking(){}function setBusy(){}
function setComposerStatus(){}function showToast(){}function renderSessionList(){}
function queueSessionMessage(){calls.push('queue')}
function _restoreComposerDraftAfterFailedSend(t,f,sid,p,id){calls.push(['restore',t,f.map(x=>x.name),sid,id])}
(function(){%s})();
console.log(JSON.stringify({calls,inflight:'s1' in INFLIGHT}));
""" % (json.dumps(label), body)
    out = json.loads(subprocess.run(["node", "-e", script], capture_output=True, text=True, check=True).stdout)
    assert out["inflight"] is False
    assert ["restore", "continue\nplus my edits", ["user-notes.txt"], "s1", ""] in out["calls"]
    assert "queue" not in out["calls"], "a refused continuation must never auto-resend"
    # Draft clear happens before restore, so the restore is not overwritten.
    assert out["calls"].index("clearInflight") < len(out["calls"]) - 1

@pytest.mark.parametrize("label", ["goal_continuation_stale", "goal_continuation_already_dispatched"])
def test_refused_continuation_does_not_drain_queued_followup_over_restored_draft(label):
    """Real setBusy + real rejection branch: a queued follow-up must not overwrite the restored draft."""
    import json, shutil, subprocess
    from pathlib import Path

    if not shutil.which("node"):
        pytest.skip("node not available")
    root = Path(__file__).parents[1] / "static"
    msgs = root.joinpath("messages.js").read_text(encoding="utf-8")
    ui = root.joinpath("ui.js").read_text(encoding="utf-8")
    start = msgs.index("    // Refused continuation token:")
    body = msgs[start : msgs.index("      return;\n    }", start)] + "      return;\n    }"
    set_busy = ui[ui.index("function setBusy(") : ui.index("// ── Queue chip display")]
    script = """
const INFLIGHT={s1:1};const activeSid='s1';const e={status:409};const errMsg=%s;
const _failedSendDraftText='edited text';const _failedSendFilesSnapshot=[{name:'user-notes.txt'}];
const _composerDraftClearPromise=Promise.resolve();
let _queueDrainSid=null;const sent=[];const el={value:''};
const S={busy:true,pendingFiles:[],session:{session_id:'s1',model:'m'}};
const Q=[{text:'queued followup',files:[]}];
function shiftQueuedSessionMessage(){return Q.shift()||null}function queueSessionMessage(_s,p){Q.push(p)}
const $=id=>id==='msg'?el:null;
function updateSendBtn(){}function setStatus(){}function setComposerStatus(){}function updateQueueBadge(){}
function autoResize(){}function renderTray(){}function showToast(){}function renderSessionList(){}
function clearInflightState(){}function removeThinking(){}
function send(){sent.push(el.value);el.value='';S.pendingFiles=[]}
function _restoreComposerDraftAfterFailedSend(t,f){el.value=t;S.pendingFiles=f}
%s
(function(){%s})();
setTimeout(()=>console.log(JSON.stringify({text:el.value,files:S.pendingFiles.map(x=>x.name),sent,queued:Q.map(x=>x.text),busy:S.busy})),400);
""" % (json.dumps(label), set_busy, body)
    out = json.loads(subprocess.run(["node", "-e", script], capture_output=True, text=True, check=True).stdout)
    assert out["text"] == "edited text"
    assert out["files"] == ["user-notes.txt"]
    assert out["sent"] == []
    assert out["queued"] == ["queued followup"]
    assert out["busy"] is False


def test_waiting_goal_event_never_uses_the_templated_continuing_key():
    """A wait decision has no turn args, so the goal_continuing template would render 'undefined'."""
    from api.goals import goal_message_key

    wait = {"verdict": "wait", "message": "⏳ Goal waiting on 5 running subagent(s); resumes when they finish.",
            "message_key": "", "message_args": []}
    assert goal_message_key(wait) == ""
    assert goal_message_key({"message_key": "", "message_args": [3, 100, "r"]}) == "goal_continuing"
    assert goal_message_key({"message_key": "goal_done", "message_args": []}) == "goal_done"


def test_client_keeps_server_text_when_template_renders_undefined():
    from pathlib import Path

    src = (Path(__file__).resolve().parents[1] / "static" / "messages.js").read_text()
    body = src[src.index("function _resolveGoalMessage"):][:700]
    assert "undefined" in body and "return raw" in body


@pytest.mark.parametrize("lineage", ["sealed", "raises"])
def test_goal_wakeup_admission_rejects_parent_compressed_after_preflight(
    _admission_env, tmp_path, monkeypatch, lineage
):
    """A compression committed between preflight and dispatch must not start a turn on the parent."""
    from api import compression_continuation

    s, _ = _admission_env
    cid = webui_goals.register_pending_goal_continuation(s.session_id, "continue the goal")

    def _lineage(_s):
        if lineage == "raises":
            raise RuntimeError("state.db locked")
        return True, "tip-sid"

    monkeypatch.setattr(compression_continuation, "durable_compression_continuation", _lineage)
    resp = _admit(s, tmp_path, cid)
    assert resp["_status"] == 409
    assert resp["error"] == "goal_continuation_rejected"
    assert resp["reason"] == ("sealed_parent" if lineage == "sealed" else "lineage_unknown")
    assert not getattr(s, "active_stream_id", None)
    assert not getattr(s, "pending_user_message", None)
    # Rejected before the token was consumed: the record is untouched.
    assert webui_goals.peek_pending_goal_continuation_id(s.session_id) == cid


def test_sealed_parent_retirement_leaves_visible_stopped_outcome(monkeypatch):
    """Dropping the parent's record must pause the tip's goal with a reason naming compression + child."""
    from api import compression_continuation

    s = _sess(session_id="sid-parent")
    _detached_env(monkeypatch, s)
    monkeypatch.setattr(
        compression_continuation, "durable_compression_continuation", lambda _s: (True, "sid-child")
    )
    paused = []

    class Mgr:
        def is_active(self):
            return True

        def pause(self, reason=""):
            paused.append(reason)

    managers = []
    monkeypatch.setattr(
        webui_goals, "_manager", lambda sid, profile_home=None: managers.append(sid) or Mgr()
    )
    webui_goals.retire_continuation_for_sealed_parent(s.session_id)
    assert not webui_goals.pending_goal_continuation_matches(s.session_id, s.cid)
    assert managers == ["sid-child"]
    assert len(paused) == 1
    assert "compressed" in paused[0] and "sid-parent" in paused[0] and "sid-child" in paused[0]
