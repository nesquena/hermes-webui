"""Regression tests for /goal continuation across Gateway turns, subagent wakeups, and resume."""

from __future__ import annotations

from collections import OrderedDict
from types import SimpleNamespace

import pytest

from api import gateway_chat, goals as webui_goals, models, routes
from api.config import PENDING_GOAL_CONTINUATION, STREAMS, create_stream_channel
from api.models import new_session


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
    """#7855 contract: pause keeps an existing record, resume keeps it (no new
    kickoff) and its ID is still admitted goal-related; clear retires it."""
    session, _holder, kickoffs = _goal_command_env(monkeypatch, tmp_path, "sid-goal-resume")
    webui_goals.clear_pending_goal_continuation(session.session_id)
    cid = webui_goals.register_pending_goal_continuation(session.session_id, "continue the goal")
    try:
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
    finally:
        webui_goals.clear_pending_goal_continuation(session.session_id)


def test_goal_resume_without_pending_record_kicks_off_goal_related_turn(monkeypatch, tmp_path):
    """Should fix 1: /goal resume with no pending record re-arms the loop with a
    goal-related kickoff of the next continuation prompt (no bare set write)."""
    session, holder, kickoffs = _goal_command_env(monkeypatch, tmp_path, "sid-goal-resume-kick")
    webui_goals.clear_pending_goal_continuation(session.session_id)
    holder["state"].status = "paused"
    try:
        res = routes._handle_goal_command(object(), {"session_id": session.session_id, "args": "resume"})
        assert res["status"] == 200
        assert res["payload"]["stream_id"] == "resume-kickoff"
        assert len(kickoffs) == 1
        assert kickoffs[0]["goal_related"] is True
        assert "replace helpers with native code" in kickoffs[0]["msg"]
        assert session.session_id not in PENDING_GOAL_CONTINUATION
    finally:
        webui_goals.clear_pending_goal_continuation(session.session_id)


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


def _gateway_in_turn_goal_env(tmp_path, monkeypatch, *, stream_id):
    """Session + a fake native GoalManager whose in-turn evaluation already
    persisted to ``state.db`` (``last_turn_at`` in the future ⇒ the Gateway
    evaluated this turn's goal, the WebUI did not)."""
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
            yield b'data: {"choices":[{"delta":{"content":"wave 3 merged"}}]}\n\n'
            yield b"data: [DONE]\n\n"

    monkeypatch.setenv("HERMES_WEBUI_GATEWAY_BASE_URL", "http://gateway.local")
    monkeypatch.setattr(gateway_chat.urllib.request, "urlopen", lambda req, timeout=0: FakeResponse())

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
    """Review 5603344218 Must fix 1: when the Gateway already evaluated the goal
    in-turn, the WebUI adopts that decision (no second judge) but only to REPORT
    status. The turn was not goal-related, so the native Agent owns the
    continuation: no browser ``goal_continue`` and no record."""
    stream_id = "stream-gateway-in-turn-goal"
    s, channel, fake_mgr, eval_calls = _gateway_in_turn_goal_env(
        tmp_path, monkeypatch, stream_id=stream_id
    )
    subscriber = channel.subscribe()

    try:
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
        # The Agent's own post-turn hook dispatched it; a WebUI dispatch would
        # be the second continuation the review reproduced.
        assert "goal_continue" not in event_names
        assert s.session_id not in PENDING_GOAL_CONTINUATION
        goal_messages = [
            str(item[1].get("message") or "") for item in events if item[0] == "goal"
        ]
        assert any("1/100" in memo for memo in goal_messages), goal_messages
    finally:
        PENDING_GOAL_CONTINUATION.discard(s.session_id)
        webui_goals.clear_pending_goal_continuation(s.session_id)


def test_gateway_turn_with_native_hook_and_goal_related_false_launches_nothing(
    tmp_path, monkeypatch
):
    """Review 5603344218 Must fix 1 regression: ONE Gateway turn whose native
    post-turn hook owns the continuation (``goal_related=False``) gives ZERO
    WebUI launches — no detached ``start_session_turn``, no browser dispatch,
    no record (and therefore nothing for a late subscriber to replay)."""
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

    try:
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
    finally:
        PENDING_GOAL_CONTINUATION.discard(s.session_id)
        webui_goals.clear_pending_goal_continuation(s.session_id)


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

    try:
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
    finally:
        PENDING_GOAL_CONTINUATION.discard(s.session_id)


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
    try:
        assert webui_goals.detached_goal_continuation_skip_reason(s.session_id, s.cid) == ""
        assert homes == ["alice"]
        # The reader checks the record AND its ID, not set membership.
        assert webui_goals.detached_goal_continuation_skip_reason(s.session_id, "0" * 32) == "not_pending"
    finally:
        webui_goals.clear_pending_goal_continuation(s.session_id)


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
    try:
        assert webui_goals.detached_goal_continuation_skip_reason(s.session_id, s.cid) == expected
    finally:
        webui_goals.clear_pending_goal_continuation(s.session_id)


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


@pytest.mark.parametrize("status", ["cleared", "", "bogus"])
def test_in_turn_adoption_never_continues_a_cleared_or_unknown_goal(status):
    """Must fix 1: a goal cleared between the in-turn evaluation and the hook read
    must not produce a continuation."""
    st = _EvalState()
    st.status = status
    decision = webui_goals._decision_from_evaluated_state(_EvalMgr(st), st)
    assert decision is not None
    assert decision["should_continue"] is False
    assert decision["continuation_prompt"] is None


def test_in_turn_adoption_uses_live_wait_state_not_stale_wait_verdict():
    """Must fix 4: resume cleared the barrier but left last_verdict="wait"."""
    st = _EvalState()
    st.last_verdict = "wait"
    decision = webui_goals._decision_from_evaluated_state(_EvalMgr(st, waiting=False), st)
    assert decision["should_continue"] is True
    parked = webui_goals._decision_from_evaluated_state(_EvalMgr(st, waiting=True), st)
    assert parked["should_continue"] is False


def test_in_turn_adoption_rebuilds_failed_quality_gate_after_resume(monkeypatch):
    """Must fix 4: the native turn was gate_failed/True (last_verdict untouched,
    still the stale "wait"); adoption must reproduce gate_failed/True with the
    failed-gate prompt, not park the goal."""
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
    """Must fix 2: the detached admission consumes the record by ID, so the same
    ID cannot be admitted goal-related a second time."""
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
    """Must fix 3: a pause/clear that lands after the pre-flight check makes the
    admission reject the goal_wakeup (409), never run it as goal_related=False."""
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
    """Review 5603344218 Must fix 2: with no SSE subscriber the record keeps
    dispatcher ownership. A pause landing after the preflight makes admission
    409 and, before this fix, left the record pending with nothing to run it:
    both resumes returned 200 with 0 kickoffs and 0 replayable events. Resume
    must retry that SAME continuation ID — exactly one recoverable
    goal-related dispatch, no second dispatcher, record consumed."""
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
        """Admission: rejected while the goal is paused, otherwise it consumes
        the record by ID — the same contract as the real route."""
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
    # Real preflight, with the session/model/profile seams stubbed: the record
    # exists, the goal is active after resume, no turn is queued or running.
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
        # The detached handoff already preflighted; the pause then lands before
        # admission, so the wakeup is rejected and nothing dispatches.
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
    """Must fix 2, idle half: a turn that ends without handing off a
    continuation retries a WebUI-owned (detached) record left by a stopped
    runner — its preflight skipped a queued/active turn, say — with its
    existing ID. A browser-owned record is never re-dispatched."""
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
    try:
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
    finally:
        PENDING_GOAL_CONTINUATION.discard(s.session_id)
        webui_goals.clear_pending_goal_continuation(s.session_id)


def test_retry_detached_goal_continuation_binds_ownership_to_the_id(monkeypatch):
    """Must fix 2: ownership is bound to the continuation ID, and retries reuse
    that ID so admission stays the single arbiter."""
    sid = "sid-detached-ownership"
    webui_goals.clear_pending_goal_continuation(sid)
    started = []
    monkeypatch.setattr(
        webui_goals,
        "start_detached_goal_continuation",
        lambda s, prompt, cont_id: started.append((s, prompt, cont_id)),
    )
    try:
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
    finally:
        webui_goals.clear_pending_goal_continuation(sid)
