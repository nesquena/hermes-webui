"""Regression tests for first-class WebUI /goal command parity."""

import io
import json
import re
import threading
from pathlib import Path
from types import SimpleNamespace

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
COMMANDS_JS = (REPO_ROOT / "static" / "commands.js").read_text(encoding="utf-8")
MESSAGES_JS = (REPO_ROOT / "static" / "messages.js").read_text(encoding="utf-8")
ROUTES_PY = (REPO_ROOT / "api" / "routes.py").read_text(encoding="utf-8")
STREAMING_PY = (REPO_ROOT / "api" / "streaming.py").read_text(encoding="utf-8")


def test_goal_command_payload_matches_gateway_controls(monkeypatch):
    """The backend command helper mirrors gateway /goal status/pause/resume/clear/set."""
    from api import goals as webui_goals

    calls = []

    class FakeState:
        goal = "ship the feature"
        status = "active"
        turns_used = 0
        max_turns = 20
        last_verdict = None
        last_reason = None
        paused_reason = None

    class FakeGoalManager:
        def __init__(self, session_id, default_max_turns=20):
            calls.append(("init", session_id, default_max_turns))
            self.state = None

        def status_line(self):
            return "No active goal. Set one with /goal <text>."

        def pause(self, reason="user-paused"):
            calls.append(("pause", reason))
            return FakeState()

        def resume(self, reset_budget=True):
            calls.append(("resume", reset_budget))
            return FakeState()

        def has_goal(self):
            return True

        def clear(self):
            calls.append(("clear",))

        def set(self, goal):
            calls.append(("set", goal))
            state = FakeState()
            state.goal = goal
            self.state = state
            return state

    monkeypatch.setattr(webui_goals, "GoalManager", FakeGoalManager)
    monkeypatch.setattr(webui_goals, "_default_max_turns", lambda: 20)

    status = webui_goals.goal_command_payload("sid-123", "status")
    pause = webui_goals.goal_command_payload("sid-123", "pause")
    resume = webui_goals.goal_command_payload("sid-123", "resume")
    clear = webui_goals.goal_command_payload("sid-123", "clear")
    set_goal = webui_goals.goal_command_payload("sid-123", "ship the feature")

    assert status["message"] == "No active goal. Set one with /goal <text>."
    assert status["message_key"] == "goal_status_none"
    assert pause["message"] == "⏸ Goal paused: ship the feature"
    assert pause["message_key"] == "goal_paused"
    assert pause["message_args"] == ["ship the feature"]
    assert resume["message"].startswith("▶ Goal resumed: ship the feature")
    assert resume["message_key"] == "goal_resumed"
    assert resume["message_args"] == ["ship the feature"]
    assert clear["message"] == "Goal cleared."
    assert clear["message_key"] == "goal_cleared"
    assert set_goal["action"] == "set"
    assert set_goal["message_key"] == "goal_set"
    assert set_goal["message_args"] == [20, "ship the feature"]
    assert set_goal["kickoff_prompt"] == "ship the feature"
    assert "⊙ Goal set (20-turn budget): ship the feature" in set_goal["message"]
    assert ("set", "ship the feature") in calls


def test_goal_command_payload_rejects_new_goal_while_stream_running(monkeypatch):
    """Status/control subcommands are safe mid-run; replacing the goal is not."""
    from api import goals as webui_goals

    class FakeGoalManager:
        def __init__(self, session_id, default_max_turns=20):
            pass

        def status_line(self):
            return "⊙ Goal (active, 1/20 turns): existing"

    monkeypatch.setattr(webui_goals, "GoalManager", FakeGoalManager)
    monkeypatch.setattr(webui_goals, "_default_max_turns", lambda: 20)

    status = webui_goals.goal_command_payload("sid-123", "status", stream_running=True)
    rejected = webui_goals.goal_command_payload("sid-123", "replace it", stream_running=True)

    assert status["ok"] is True
    assert rejected["ok"] is False
    assert rejected["error"] == "agent_running"
    assert "use /goal status / pause / clear mid-run" in rejected["message"]


def test_has_active_goal_reports_only_active_state(monkeypatch):
    """Streaming can avoid showing an evaluating spinner when no standing goal is active."""
    from api import goals as webui_goals

    class FakeGoalManager:
        def __init__(self, session_id, default_max_turns=20):
            self.session_id = session_id

        def is_active(self):
            return self.session_id == "sid-active-goal"

    monkeypatch.setattr(webui_goals, "GoalManager", FakeGoalManager)
    monkeypatch.setattr(webui_goals, "_default_max_turns", lambda: 20)

    assert webui_goals.has_active_goal("sid-active-goal") is True
    assert webui_goals.has_active_goal("sid-idle-goal") is False
    assert webui_goals.has_active_goal("") is False


def test_profile_goal_evaluation_supports_native_judge_contract(monkeypatch, tmp_path):
    """Profile goals accept the current five-field native judge result."""
    from api import goals as webui_goals
    native_goals = pytest.importorskip("hermes_cli.goals", reason="hermes-agent not installed")

    judge = lambda *args, **kwargs: ("continue", "work remains", False, None, False)
    monkeypatch.setattr(webui_goals, "judge_goal", judge)
    monkeypatch.setattr(native_goals, "judge_goal", judge)
    monkeypatch.syspath_prepend(str(Path(native_goals.__file__).resolve().parents[1]))
    from hermes_constants import get_hermes_home

    original_home = get_hermes_home()
    webui_goals._DB_CACHE.clear()
    native_goals._DB_CACHE.clear()
    __import__("hermes_state")

    home = tmp_path / "profile"
    webui_goals.goal_command_payload("sid-native-contract", "finish it", profile_home=home)
    decision = webui_goals.evaluate_goal_after_turn(
        "sid-native-contract",
        "not finished",
        profile_home=home,
    )
    state = webui_goals.goal_state_snapshot("sid-native-contract", profile_home=home)

    assert decision["verdict"] == "continue"
    assert decision["should_continue"] is True
    assert state.turns_used == 1
    assert state.last_verdict == "continue"

    snapshot = webui_goals.goal_state_snapshot("sid-native-contract", profile_home=home)
    webui_goals.goal_command_payload("sid-native-contract", "replacement", profile_home=home)
    webui_goals.restore_goal_state("sid-native-contract", snapshot, profile_home=home)
    restored = webui_goals.goal_state_snapshot("sid-native-contract", profile_home=home)
    assert restored.goal == "finish it"
    assert restored.turns_used == 1
    assert get_hermes_home() == original_home


def test_profile_goal_evaluation_preserves_native_wait_semantics(monkeypatch, tmp_path):
    """Profile goals park when the native judge returns a wait directive."""
    from api import goals as webui_goals
    native_goals = pytest.importorskip("hermes_cli.goals", reason="hermes-agent not installed")

    judge = lambda *args, **kwargs: (
        "wait",
        "rate limited",
        False,
        {"seconds": 30},
        False,
    )
    monkeypatch.setattr(webui_goals, "judge_goal", judge)
    monkeypatch.setattr(native_goals, "judge_goal", judge)
    monkeypatch.syspath_prepend(str(Path(native_goals.__file__).resolve().parents[1]))
    webui_goals._DB_CACHE.clear()
    native_goals._DB_CACHE.clear()
    __import__("hermes_state")

    home = tmp_path / "profile"
    webui_goals.goal_command_payload("sid-native-wait", "finish it", profile_home=home)
    decision = webui_goals.evaluate_goal_after_turn(
        "sid-native-wait",
        "rate limited",
        profile_home=home,
    )
    state = webui_goals.goal_state_snapshot("sid-native-wait", profile_home=home)

    assert decision["verdict"] == "wait"
    assert decision["should_continue"] is False
    assert state.waiting_until > 0
    assert state.waiting_reason == "rate limited"


def test_profile_goal_context_isolated_across_threads(monkeypatch, tmp_path):
    """Concurrent profiles with the same session id cannot cross-write goals."""
    from api import goals as webui_goals
    native_goals = pytest.importorskip("hermes_cli.goals", reason="hermes-agent not installed")

    monkeypatch.syspath_prepend(str(Path(native_goals.__file__).resolve().parents[1]))
    from hermes_constants import get_hermes_home

    original_home = get_hermes_home()
    webui_goals._DB_CACHE.clear()
    native_goals._DB_CACHE.clear()
    __import__("hermes_state")
    barrier = threading.Barrier(2)
    failures = []

    def set_goal(profile):
        try:
            barrier.wait()
            result = webui_goals.goal_command_payload(
                "shared-session",
                f"goal-{profile}",
                profile_home=tmp_path / profile,
            )
            assert result["ok"] is True
        except Exception as exc:
            failures.append(exc)

    threads = [threading.Thread(target=set_goal, args=(profile,)) for profile in ("a", "b")]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    assert failures == []
    assert webui_goals.goal_state_snapshot(
        "shared-session", profile_home=tmp_path / "a"
    ).goal == "goal-a"
    assert webui_goals.goal_state_snapshot(
        "shared-session", profile_home=tmp_path / "b"
    ).goal == "goal-b"
    assert get_hermes_home() == original_home


def test_profile_goal_falls_back_when_session_db_path_is_frozen(monkeypatch, tmp_path):
    """A context API alone cannot make an import-time SessionDB path profile-safe."""
    from api import goals as webui_goals
    native_goals = pytest.importorskip("hermes_cli.goals", reason="hermes-agent not installed")
    import hermes_state

    frozen_db_path = tmp_path / "frozen-home" / "state.db"
    monkeypatch.setattr(hermes_state, "DEFAULT_DB_PATH", frozen_db_path)
    webui_goals._DB_CACHE.clear()
    native_goals._DB_CACHE.clear()

    profile_a = tmp_path / "profile-a"
    profile_b = tmp_path / "profile-b"
    try:
        assert webui_goals.goal_command_payload(
            "shared-session", "goal-a", profile_home=profile_a
        )["ok"] is True
        assert webui_goals.goal_command_payload(
            "shared-session", "goal-b", profile_home=profile_b
        )["ok"] is True

        assert webui_goals.goal_state_snapshot(
            "shared-session", profile_home=profile_a
        ).goal == "goal-a"
        assert webui_goals.goal_state_snapshot(
            "shared-session", profile_home=profile_b
        ).goal == "goal-b"
    finally:
        for cache in (webui_goals._DB_CACHE, native_goals._DB_CACHE):
            for db in cache.values():
                close = getattr(db, "close", None)
                if close is not None:
                    close()
            cache.clear()


def test_profile_goal_context_resets_when_native_call_raises(monkeypatch, tmp_path):
    """A failed delegated call cannot leak its profile into the next task."""
    from api import goals as webui_goals
    native_goals = pytest.importorskip("hermes_cli.goals", reason="hermes-agent not installed")

    monkeypatch.syspath_prepend(str(Path(native_goals.__file__).resolve().parents[1]))
    from hermes_constants import (
        get_hermes_home,
        reset_hermes_home_override,
        set_hermes_home_override,
    )

    class RaisingGoalManager:
        state = None

        def __init__(self, session_id, default_max_turns=20):
            self.session_id = session_id
            self.default_max_turns = default_max_turns

        def explode(self):
            raise RuntimeError("boom")

    original_home = get_hermes_home()
    monkeypatch.setattr(webui_goals, "_NativeGoalManager", RaisingGoalManager)
    manager = webui_goals._ProfileGoalManager(
        "sid-context-reset",
        profile_home=tmp_path / "profile",
        context_api=(set_hermes_home_override, reset_hermes_home_override),
    )

    with pytest.raises(RuntimeError, match="boom"):
        manager.explode()

    assert get_hermes_home() == original_home


def test_goal_continuation_decision_emits_status_and_normal_user_prompt(monkeypatch):
    """Post-turn hook returns the visible status event plus a normal continuation prompt."""
    from api import goals as webui_goals

    class FakeGoalManager:
        def __init__(self, session_id, default_max_turns=20):
            self.session_id = session_id

        def is_active(self):
            return True

        def evaluate_after_turn(self, last_response, user_initiated=True):
            return {
                "status": "active",
                "should_continue": True,
                "continuation_prompt": "[Continuing toward your standing goal]\nGoal: ship it",
                "verdict": "continue",
                "reason": "one step remains",
                "message": "↻ Continuing toward goal (1/20): one step remains",
            }

    monkeypatch.setattr(webui_goals, "GoalManager", FakeGoalManager)
    monkeypatch.setattr(webui_goals, "_default_max_turns", lambda: 20)

    decision = webui_goals.evaluate_goal_after_turn("sid-123", "not done yet", user_initiated=False)

    assert decision["message_key"] == "goal_continuing"
    assert decision["message_args"] == [1, 20, "one step remains"]
    assert decision["message"].startswith("↻ Continuing toward goal")
    assert decision["should_continue"] is True
    assert decision["continuation_prompt"].startswith("[Continuing toward your standing goal]")


@pytest.mark.parametrize("gateway_owned", [False, True])
def test_goal_endpoint_sets_goal_and_starts_kickoff_stream(
    monkeypatch, tmp_path, gateway_owned
):
    """POST /api/goal uses GoalManager state and launches the first goal turn."""
    from api import goals as webui_goals
    from api import routes

    class FakeState:
        goal = "ship the feature"
        status = "active"
        turns_used = 0
        max_turns = 20
        last_verdict = None
        last_reason = None
        paused_reason = None

    class FakeGoalManager:
        def __init__(self, session_id, default_max_turns=20):
            self.session_id = session_id
            self.default_max_turns = default_max_turns

        def set(self, goal):
            state = FakeState()
            state.goal = goal
            return state

    class FakeSession:
        session_id = "sid-goal-route"
        profile = "default"
        workspace = str(tmp_path)
        model = "gpt-5.5"
        model_provider = "openai-codex"
        messages = []
        context_messages = []
        pending_user_message = None
        active_stream_id = None

    monkeypatch.setattr(webui_goals, "GoalManager", FakeGoalManager)
    monkeypatch.setattr(routes, "get_session", lambda sid: FakeSession())
    monkeypatch.setattr(routes, "resolve_trusted_workspace", lambda workspace: tmp_path)
    monkeypatch.setattr(
        routes,
        "webui_gateway_chat_enabled",
        lambda _cfg: gateway_owned,
    )
    monkeypatch.setattr(routes, "get_config", lambda: {})
    monkeypatch.setattr(
        routes,
        "_resolve_compatible_session_model_state",
        lambda model, provider, **_: (model, provider, False),
    )
    started = []

    def fake_start(session, **kwargs):
        started.append(kwargs)
        return {"stream_id": "goal-stream", "session_id": session.session_id, "pending_started_at": 123.0}

    monkeypatch.setattr(routes, "_start_chat_stream_for_session", fake_start)
    monkeypatch.setattr(routes, "j", lambda handler, payload, status=200, **kwargs: {"status": status, "payload": payload})

    result = routes._handle_goal_command(
        object(),
        {
            "session_id": "sid-goal-route",
            "args": "ship the feature",
            "workspace": str(tmp_path),
            "model": "gpt-5.5",
            "model_provider": "openai-codex",
        },
    )

    assert result["status"] == 200
    assert result["payload"]["action"] == "set"
    assert result["payload"]["stream_id"] == "goal-stream"
    assert started and started[0]["msg"] == "ship the feature"
    assert started[0]["model_provider"] == "openai-codex"
    assert started[0]["external_runtime_owned"] is gateway_owned


def test_goal_endpoint_preserves_response_shape_under_runtime_adapter_flag(monkeypatch, tmp_path):
    """The Slice 3c adapter path delegates /goal without adding adapter-only fields."""
    from api import goals as webui_goals
    from api import routes

    class FakeState:
        goal = "ship the feature"
        status = "active"
        turns_used = 1
        max_turns = 20
        last_verdict = None
        last_reason = None
        paused_reason = None

    class FakeGoalManager:
        def __init__(self, session_id, default_max_turns=20):
            self.state = FakeState()

    class FakeSession:
        session_id = "sid-goal-route"
        profile = "default"
        workspace = str(tmp_path)
        model = "gpt-5.5"
        model_provider = "openai-codex"
        messages = []
        context_messages = []
        pending_user_message = None
        active_stream_id = None

    monkeypatch.setenv("HERMES_WEBUI_RUNTIME_ADAPTER", "legacy-journal")
    monkeypatch.setattr(webui_goals, "GoalManager", FakeGoalManager)
    monkeypatch.setattr(routes, "get_session", lambda sid: FakeSession())
    monkeypatch.setattr(routes, "j", lambda handler, payload, status=200, **kwargs: {"status": status, "payload": payload})

    result = routes._handle_goal_command(object(), {"session_id": "sid-goal-route", "args": "status"})

    assert result["status"] == 200
    assert result["payload"]["action"] == "status"
    assert result["payload"]["message_key"] == "goal_status_active"
    assert "run_id" not in result["payload"]
    assert "active_controls" not in result["payload"]


def test_goal_endpoint_adapter_keeps_full_set_text_and_legacy_payload_status(monkeypatch, tmp_path):
    """The adapter action label must not replace legacy parsing of full goal text."""
    from api import goals as webui_goals
    from api import routes

    set_calls = []

    class FakeState:
        goal = ""
        status = "active"
        turns_used = 0
        max_turns = 20
        last_verdict = None
        last_reason = None
        paused_reason = None

    class FakeGoalManager:
        def __init__(self, session_id, default_max_turns=20):
            self.state = FakeState()

        def set(self, text):
            set_calls.append(text)
            self.state.goal = text
            return self.state

    class FakeSession:
        session_id = "sid-goal-route"
        profile = "default"
        workspace = str(tmp_path)
        model = "gpt-5.5"
        model_provider = "openai-codex"
        messages = []
        context_messages = []
        pending_user_message = None
        active_stream_id = None

    monkeypatch.setenv("HERMES_WEBUI_RUNTIME_ADAPTER", "legacy-journal")
    monkeypatch.setattr(webui_goals, "GoalManager", FakeGoalManager)
    monkeypatch.setattr(routes, "get_session", lambda sid: FakeSession())
    monkeypatch.setattr(routes, "resolve_trusted_workspace", lambda workspace: tmp_path)
    monkeypatch.setattr(
        routes,
        "_resolve_compatible_session_model_state",
        lambda model, provider, **_: (model, provider, False),
    )
    monkeypatch.setattr(
        routes,
        "_start_chat_stream_for_session",
        lambda session, **kwargs: {"stream_id": "goal-stream", "session_id": session.session_id},
    )
    monkeypatch.setattr(routes, "j", lambda handler, payload, status=200, **kwargs: {"status": status, "payload": payload})

    result = routes._handle_goal_command(object(), {"session_id": "sid-goal-route", "args": "set foo"})

    assert result["status"] == 200
    assert result["payload"]["action"] == "set"
    assert result["payload"]["kickoff_prompt"] == "set foo"
    assert set_calls == ["set foo"]


def test_goal_endpoint_adapter_error_payload_still_controls_http_status(monkeypatch, tmp_path):
    """The /goal route preserves legacy error/status handling under the adapter flag."""
    from api import goals as webui_goals
    from api import routes

    class FakeGoalManager:
        state = None

        def __init__(self, session_id, default_max_turns=20):
            pass

    class FakeSession:
        session_id = "sid-goal-route"
        profile = "default"
        workspace = str(tmp_path)
        model = "gpt-5.5"
        model_provider = "openai-codex"
        messages = []
        context_messages = []
        pending_user_message = None
        active_stream_id = "running-stream"

    monkeypatch.setenv("HERMES_WEBUI_RUNTIME_ADAPTER", "legacy-journal")
    monkeypatch.setattr(webui_goals, "GoalManager", FakeGoalManager)
    monkeypatch.setattr(routes, "get_session", lambda sid: FakeSession())
    monkeypatch.setitem(routes.STREAMS, "running-stream", {"queue": object()})
    monkeypatch.setattr(routes, "j", lambda handler, payload, status=200, **kwargs: {"status": status, "payload": payload})

    result = routes._handle_goal_command(object(), {"session_id": "sid-goal-route", "args": "ship it"})

    assert result["status"] == 409
    assert result["payload"]["ok"] is False
    assert result["payload"]["error"] == "agent_running"


def test_routes_register_goal_endpoint_and_kickoff_stream():
    assert 'if parsed.path == "/api/goal"' in ROUTES_PY
    assert "return _handle_goal_command(handler, body)" in ROUTES_PY
    assert "goal_command_payload" in ROUTES_PY
    assert "kickoff_prompt" in ROUTES_PY
    assert "_start_chat_stream_for_session" in ROUTES_PY


def test_chat_start_forwards_goal_related_to_gateway_worker(monkeypatch, tmp_path):
    from api import routes
    import api.turn_journal as turn_journal

    class FakeSession:
        session_id = "sid-goal-related-gateway"
        active_stream_id = None
        pending_started_at = 0.0
        title = "Goal Chat"
        profile = "default"

    captured = {}

    class FakeThread:
        def __init__(self, *, target, args, kwargs, daemon):
            captured["target"] = target
            captured["args"] = args
            captured["kwargs"] = kwargs
            captured["daemon"] = daemon

        def start(self):
            captured["started"] = True

    def fake_prepare(session, **kwargs):
        session.pending_started_at = 123.0
        session.title = "Goal Chat"

    monkeypatch.setattr(routes, "_get_session_agent_lock", lambda *args, **kwargs: threading.Lock())
    monkeypatch.setattr(routes, "_active_stream_blocks_chat_start", lambda *args, **kwargs: False)
    monkeypatch.setattr(routes, "_active_run_stream_for_session", lambda *args, **kwargs: None)
    monkeypatch.setattr(routes, "_prepare_chat_start_session_for_stream", fake_prepare)
    monkeypatch.setattr(routes, "_is_hidden_empty_session", lambda *args, **kwargs: False)
    monkeypatch.setattr(routes, "publish_session_list_changed", lambda *args, **kwargs: None)
    monkeypatch.setattr(routes, "set_last_workspace", lambda *args, **kwargs: None)
    monkeypatch.setattr(turn_journal, "append_turn_journal_event", lambda *args, **kwargs: {})
    monkeypatch.setattr(routes, "webui_gateway_chat_enabled", lambda *args, **kwargs: True)
    monkeypatch.setattr(routes.threading, "Thread", FakeThread)
    monkeypatch.setattr(routes.uuid, "uuid4", lambda: SimpleNamespace(hex="goal-stream-id"))

    session = FakeSession()
    response = {}
    try:
        response = routes._start_chat_stream_for_session(
            session,
            msg="continue the goal",
            attachments=[],
            workspace=str(tmp_path),
            model="gpt-5.5",
            model_provider="openai-codex",
            goal_related=True,
        )

        assert response["stream_id"] == "goal-stream-id"
        assert captured["target"] is routes._run_gateway_chat_streaming
        assert captured["kwargs"]["goal_related"] is True
        assert captured["kwargs"]["model_provider"] == "openai-codex"
        assert captured["started"] is True
    finally:
        stream_id = str(response.get("stream_id") or "")
        with routes.STREAMS_LOCK:
            routes.STREAMS.pop(stream_id, None)
        routes.unregister_stream_owner(stream_id)
        routes.STREAM_GOAL_RELATED.pop(stream_id, None)
        with routes.LOCK:
            if routes.SESSIONS.get(session.session_id) is session:
                routes.SESSIONS.pop(session.session_id, None)


def test_streaming_post_turn_goal_hook_surfaces_and_continues():
    assert "evaluate_goal_after_turn" in STREAMING_PY
    assert "put('goal'" in STREAMING_PY
    assert "decision.get('should_continue')" in STREAMING_PY
    assert "continuation_prompt" in STREAMING_PY
    assert "put('goal_continue'" in STREAMING_PY
    goal_idx = STREAMING_PY.find("evaluate_goal_after_turn")
    done_idx = STREAMING_PY.find("put('done'", goal_idx)
    assert goal_idx != -1 and done_idx != -1
    assert goal_idx < done_idx, "goal status should be emitted before the terminal done payload"


def test_streaming_goal_hook_emits_evaluating_state_before_judge():
    evaluating_idx = STREAMING_PY.find("'state': 'evaluating'")
    judge_idx = STREAMING_PY.find("_goal_decision = evaluate_goal_after_turn")
    done_idx = STREAMING_PY.find("put('done'", judge_idx)
    assert evaluating_idx != -1, "goal hook should emit an evaluating state before judge round-trip"
    assert judge_idx != -1 and done_idx != -1
    assert evaluating_idx < judge_idx < done_idx
    assert "Evaluating goal progress…" in STREAMING_PY
    assert "'state': 'continuing' if decision.get('should_continue') else 'idle'" in STREAMING_PY


def test_frontend_has_goal_slash_command_and_status_event_handler():
    assert "{name:'goal'" in COMMANDS_JS
    assert "subArgs:['status','pause','resume','clear']" in COMMANDS_JS
    assert "function cmdGoal" in COMMANDS_JS
    assert "api('/api/goal'" in COMMANDS_JS
    assert "stream_id" in COMMANDS_JS
    assert "goal'" in MESSAGES_JS
    assert "source.addEventListener('goal'" in MESSAGES_JS
    assert "source.addEventListener('goal_continue'" in MESSAGES_JS
    assert "['steer','interrupt','queue','terminal','goal','yolo'].includes(_pc.name)" in MESSAGES_JS
    assert "queueSessionMessage" in MESSAGES_JS


def test_frontend_goal_evaluating_state_uses_calm_composer_indicator():
    assert "const goalState=String(d.state||'').trim();" in MESSAGES_JS
    assert "t('goal_evaluating_progress')" in MESSAGES_JS
    assert "if(goalState==='evaluating')" in MESSAGES_JS
    assert "setComposerStatus(goalEvaluatingMessage);" in MESSAGES_JS
    assert "return;" in MESSAGES_JS


def test_goal_kickoff_forwards_explicit_model_pick_to_resolver(monkeypatch, tmp_path):
    """#6703: /api/goal must carry explicit_model_pick to the model resolver.

    Without it a persisted cross-provider pick (e.g. provider1:model1 chosen
    for the session) is treated as stale by _resolve_compatible_session_model_state
    and "repaired" back to the profile default mid-session, switching the
    provider while /goal runs.
    """
    from api import goals as webui_goals
    from api import routes

    class FakeState:
        goal = "ship the feature"
        status = "active"
        turns_used = 0
        max_turns = 20
        last_verdict = None
        last_reason = None
        paused_reason = None

    class FakeGoalManager:
        def __init__(self, session_id, default_max_turns=20):
            self.session_id = session_id
            self.default_max_turns = default_max_turns

        def set(self, goal):
            state = FakeState()
            state.goal = goal
            return state

    class FakeSession:
        session_id = "sid-goal-explicit"
        profile = "default"
        workspace = str(tmp_path)
        model = "deepseek/deepseek-v4-flash"
        model_provider = "deepseek"
        messages = []
        context_messages = []
        pending_user_message = None
        active_stream_id = None

    resolver_kwargs = {}

    def fake_resolve(model, provider, **kwargs):
        resolver_kwargs.update(kwargs)
        return model, provider, False

    monkeypatch.setattr(webui_goals, "GoalManager", FakeGoalManager)
    monkeypatch.setattr(routes, "get_session", lambda sid: FakeSession())
    monkeypatch.setattr(routes, "resolve_trusted_workspace", lambda workspace: tmp_path)
    monkeypatch.setattr(routes, "webui_gateway_chat_enabled", lambda _cfg: False)
    monkeypatch.setattr(routes, "get_config", lambda: {})
    monkeypatch.setattr(routes, "_resolve_compatible_session_model_state", fake_resolve)

    started = []

    def fake_start(session, **kwargs):
        started.append(kwargs)
        return {"stream_id": "goal-stream", "session_id": session.session_id, "pending_started_at": 123.0}

    monkeypatch.setattr(routes, "_start_chat_stream_for_session", fake_start)
    monkeypatch.setattr(routes, "j", lambda handler, payload, status=200, **kwargs: {"status": status, "payload": payload})

    result = routes._handle_goal_command(
        object(),
        {
            "session_id": "sid-goal-explicit",
            "args": "ship the feature",
            "workspace": str(tmp_path),
            "model": "deepseek/deepseek-v4-flash",
            "model_provider": "deepseek",
            "explicit_model_pick": True,
        },
    )

    assert result["status"] == 200
    assert started and started[0]["model"] == "deepseek/deepseek-v4-flash"
    assert started[0]["model_provider"] == "deepseek"
    # The explicit-pick marker must reach the resolver so the persisted
    # cross-provider model is honored instead of reverted to the default.
    assert resolver_kwargs.get("explicit_model_pick") is True


def test_goal_kickoff_defaults_explicit_model_pick_false(monkeypatch, tmp_path):
    """#6703: without the marker the resolver receives explicit_model_pick=False."""
    from api import goals as webui_goals
    from api import routes

    class FakeState:
        goal = "ship the feature"
        status = "active"
        turns_used = 0
        max_turns = 20
        last_verdict = None
        last_reason = None
        paused_reason = None

    class FakeGoalManager:
        def __init__(self, session_id, default_max_turns=20):
            self.session_id = session_id
            self.default_max_turns = default_max_turns

        def set(self, goal):
            state = FakeState()
            state.goal = goal
            return state

    class FakeSession:
        session_id = "sid-goal-default"
        profile = "default"
        workspace = str(tmp_path)
        model = "gpt-5.5"
        model_provider = "openai-codex"
        messages = []
        context_messages = []
        pending_user_message = None
        active_stream_id = None

    resolver_kwargs = {}

    def fake_resolve(model, provider, **kwargs):
        resolver_kwargs.update(kwargs)
        return model, provider, False

    monkeypatch.setattr(webui_goals, "GoalManager", FakeGoalManager)
    monkeypatch.setattr(routes, "get_session", lambda sid: FakeSession())
    monkeypatch.setattr(routes, "resolve_trusted_workspace", lambda workspace: tmp_path)
    monkeypatch.setattr(routes, "webui_gateway_chat_enabled", lambda _cfg: False)
    monkeypatch.setattr(routes, "get_config", lambda: {})
    monkeypatch.setattr(routes, "_resolve_compatible_session_model_state", fake_resolve)

    started = []

    def fake_start(session, **kwargs):
        started.append(kwargs)
        return {"stream_id": "goal-stream", "session_id": session.session_id, "pending_started_at": 123.0}

    monkeypatch.setattr(routes, "_start_chat_stream_for_session", fake_start)
    monkeypatch.setattr(routes, "j", lambda handler, payload, status=200, **kwargs: {"status": status, "payload": payload})

    result = routes._handle_goal_command(
        object(),
        {
            "session_id": "sid-goal-default",
            "args": "ship the feature",
            "workspace": str(tmp_path),
            "model": "gpt-5.5",
            "model_provider": "openai-codex",
        },
    )

    assert result["status"] == 200
    assert resolver_kwargs.get("explicit_model_pick") is False


def test_frontend_goal_sends_explicit_model_pick():
    """#6703: cmdGoal must include explicit_model_pick in the /api/goal payload.

    Mirrors the chat/start marker (messages.js) so the server honors a
    persisted cross-provider session pick instead of reverting to the default.
    """
    goal_idx = COMMANDS_JS.find("function cmdGoal")
    assert goal_idx != -1
    goal_fn = COMMANDS_JS[goal_idx : COMMANDS_JS.find("\n}", goal_idx)]
    assert "explicit_model_pick:_explicitPick" in goal_fn
    assert "api('/api/goal'" in goal_fn
    assert "_isCrossProviderPick" in goal_fn
    assert "_readPendingSessionModel" in goal_fn


def test_goal_kickoff_stamps_explicit_pick_signature(monkeypatch, tmp_path):
    """#6703: /api/goal must stamp model_explicit_pick_signature on explicit picks.

    Parity with /api/chat/start (#5979): the streaming resolver preserves a
    custom-proxy vendor namespace on a cold catalog only when the persisted
    signature matches the current routing context. Without the stamp, a first
    /goal launch after a deliberate custom-provider pick loses the vendor
    namespace and the provider reverts to the profile default mid-session.
    """
    from api import goals as webui_goals
    from api import routes

    class FakeState:
        goal = "ship the feature"
        status = "active"
        turns_used = 0
        max_turns = 20
        last_verdict = None
        last_reason = None
        paused_reason = None

    class FakeGoalManager:
        def __init__(self, session_id, default_max_turns=20):
            self.session_id = session_id
            self.default_max_turns = default_max_turns

        def set(self, goal):
            state = FakeState()
            state.goal = goal
            return state

    class FakeSession:
        session_id = "sid-goal-sig"
        profile = "default"
        workspace = str(tmp_path)
        model = "deepseek/deepseek-v4-flash"
        model_provider = "deepseek"
        messages = []
        context_messages = []
        pending_user_message = None
        active_stream_id = None
        model_explicit_pick_signature = None

    def fake_resolve(model, provider, **kwargs):
        return model, provider, False

    monkeypatch.setattr(webui_goals, "GoalManager", FakeGoalManager)
    monkeypatch.setattr(routes, "get_session", lambda sid: FakeSession())
    monkeypatch.setattr(routes, "resolve_trusted_workspace", lambda workspace: tmp_path)
    monkeypatch.setattr(routes, "webui_gateway_chat_enabled", lambda _cfg: False)
    monkeypatch.setattr(routes, "get_config", lambda: {})
    monkeypatch.setattr(routes, "_resolve_compatible_session_model_state", fake_resolve)

    def fake_start(session, **kwargs):
        return {"stream_id": "goal-stream", "session_id": session.session_id, "pending_started_at": 123.0}

    monkeypatch.setattr(routes, "_start_chat_stream_for_session", fake_start)
    monkeypatch.setattr(routes, "j", lambda handler, payload, status=200, **kwargs: {"status": status, "payload": payload})

    session = FakeSession()
    monkeypatch.setattr(routes, "get_session", lambda sid: session)

    result = routes._handle_goal_command(
        object(),
        {
            "session_id": "sid-goal-sig",
            "args": "ship the feature",
            "workspace": str(tmp_path),
            "model": "deepseek/deepseek-v4-flash",
            "model_provider": "deepseek",
            "explicit_model_pick": True,
        },
    )

    assert result["status"] == 200
    from api.models import model_explicit_pick_signature as _mk_sig

    assert session.model_explicit_pick_signature == _mk_sig("deepseek/deepseek-v4-flash", "deepseek")


def test_goal_kickoff_does_not_stamp_signature_without_explicit_pick(monkeypatch, tmp_path):
    """#6703: without the marker no signature is stamped (parity with chat/start)."""
    from api import goals as webui_goals
    from api import routes

    class FakeState:
        goal = "ship the feature"
        status = "active"
        turns_used = 0
        max_turns = 20
        last_verdict = None
        last_reason = None
        paused_reason = None

    class FakeGoalManager:
        def __init__(self, session_id, default_max_turns=20):
            self.session_id = session_id
            self.default_max_turns = default_max_turns

        def set(self, goal):
            state = FakeState()
            state.goal = goal
            return state

    class FakeSession:
        session_id = "sid-goal-nosig"
        profile = "default"
        workspace = str(tmp_path)
        model = "deepseek/deepseek-v4-flash"
        model_provider = "deepseek"
        messages = []
        context_messages = []
        pending_user_message = None
        active_stream_id = None
        model_explicit_pick_signature = "stale-previous-sig"

    def fake_resolve(model, provider, **kwargs):
        return model, provider, False

    monkeypatch.setattr(webui_goals, "GoalManager", FakeGoalManager)
    monkeypatch.setattr(routes, "resolve_trusted_workspace", lambda workspace: tmp_path)
    monkeypatch.setattr(routes, "webui_gateway_chat_enabled", lambda _cfg: False)
    monkeypatch.setattr(routes, "get_config", lambda: {})
    monkeypatch.setattr(routes, "_resolve_compatible_session_model_state", fake_resolve)

    def fake_start(session, **kwargs):
        return {"stream_id": "goal-stream", "session_id": session.session_id, "pending_started_at": 123.0}

    monkeypatch.setattr(routes, "_start_chat_stream_for_session", fake_start)
    monkeypatch.setattr(routes, "j", lambda handler, payload, status=200, **kwargs: {"status": status, "payload": payload})

    session = FakeSession()
    monkeypatch.setattr(routes, "get_session", lambda sid: session)

    result = routes._handle_goal_command(
        object(),
        {
            "session_id": "sid-goal-nosig",
            "args": "ship the feature",
            "workspace": str(tmp_path),
            "model": "deepseek/deepseek-v4-flash",
            "model_provider": "deepseek",
        },
    )

    assert result["status"] == 200
    # A non-explicit kickoff leaves any prior signature untouched (chat-start parity).
    assert session.model_explicit_pick_signature == "stale-previous-sig"


def test_frontend_goal_consumes_pending_model_marker():
    """#6703/#6705: cmdGoal must consume the one-shot pending session-model marker.

    Mirrors the chat/start path (messages.js) so a later /goal with an unchanged
    dropdown isn't treated as an explicit pick. The consume-clear is deferred to
    AFTER a successful kickoff (r.stream_id) and gated on a re-read of the stored
    marker that still matches the captured model/provider (#6705), so a control
    command (e.g. /goal status, which never produces a stream_id) leaves the
    marker intact for the next real send.
    """
    goal_idx = COMMANDS_JS.find("function cmdGoal")
    assert goal_idx != -1
    goal_fn = COMMANDS_JS[goal_idx : COMMANDS_JS.find("\n}", goal_idx)]
    assert "_clearPendingSessionModel(activeSid)" in goal_fn
    assert "_pendingPickMatch && typeof _readPendingSessionModel==='function' && typeof _clearPendingSessionModel==='function'" in goal_fn
    # The consume-clear must come AFTER the kickoff guard: a control command
    # (no stream_id) returns before reaching it.
    assert goal_fn.index("_clearPendingSessionModel(activeSid)") > goal_fn.index("if(!r||!r.stream_id)return;")
    # Re-check: the marker is re-read and only cleared while it still matches
    # the captured model/provider.
    assert "_stillPending.model===_goalModel" in goal_fn
    assert "_stillPending.model_provider" in goal_fn


def test_frontend_goal_control_command_keeps_pending_marker():
    """#6705: a control-only /goal (e.g. `/goal status`) must NOT consume the
    pending explicit-pick marker.

    The server skips model resolution for control commands (api/routes.py
    will_kickoff excludes status/pause/resume/clear/stop/done), so clearing the
    marker before the request would drop the explicit pick without using it and
    the next real send would revert to the default model. The marker must
    survive the control round-trip and still be read for the next kickoff.
    """
    goal_idx = COMMANDS_JS.find("function cmdGoal")
    assert goal_idx != -1
    goal_fn = COMMANDS_JS[goal_idx : COMMANDS_JS.find("\n}", goal_idx)]

    # 1) No clear may run before the /api/goal request: the marker must
    #    survive a control command.
    pre_request = goal_fn.split("const r=await api('/api/goal'")[0]
    assert "_clearPendingSessionModel" not in pre_request, (
        "the pending explicit-pick marker must NOT be cleared before /api/goal — "
        "a control command like /goal status never kicks off, so a pre-request "
        "clear would drop the pick without using it (#6705)"
    )

    # 2) The consume-clear sits after the kickoff guard (r.stream_id present).
    post_request = goal_fn.split("const r=await api('/api/goal'")[1]
    assert "if(!r||!r.stream_id)return;" in post_request
    assert "_clearPendingSessionModel(activeSid)" in post_request
    assert post_request.index("if(!r||!r.stream_id)return;") < post_request.index("_clearPendingSessionModel(activeSid)")

    # 3) The marker is re-read and only cleared while it still matches the
    #    captured model/provider.
    assert "_stillPending=_readPendingSessionModel(activeSid)" in post_request
    assert re.search(r"_stillPending\.model\s*===\s*_goalModel", post_request)
    assert re.search(r"_stillPending\.model_provider", post_request)
    assert re.search(r"_clearPendingSessionModel\(activeSid\)", post_request)
