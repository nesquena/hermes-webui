"""Regression tests for first-class WebUI /goal command parity."""

from pathlib import Path


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
    assert pause["message"] == "⏸ Goal paused: ship the feature"
    assert resume["message"].startswith("▶ Goal resumed: ship the feature")
    assert clear["message"] == "Goal cleared."
    assert set_goal["action"] == "set"
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
    rejected = webui_goals.goal_command_payload(
        "sid-123", "replace it", stream_running=True
    )

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

    decision = webui_goals.evaluate_goal_after_turn(
        "sid-123", "not done yet", user_initiated=False
    )

    assert decision["message"].startswith("↻ Continuing toward goal")
    assert decision["should_continue"] is True
    assert decision["continuation_prompt"].startswith(
        "[Continuing toward your standing goal]"
    )


def test_goal_endpoint_sets_goal_and_starts_kickoff_stream(monkeypatch, tmp_path):
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
        "_resolve_compatible_session_model_state",
        lambda model, provider: (model, provider, False),
    )
    started = []

    def fake_start(session, **kwargs):
        started.append(kwargs)
        return {
            "stream_id": "goal-stream",
            "session_id": session.session_id,
            "pending_started_at": 123.0,
        }

    monkeypatch.setattr(routes, "_start_chat_stream_for_session", fake_start)
    monkeypatch.setattr(
        routes,
        "j",
        lambda handler, payload, status=200, **kwargs: {
            "status": status,
            "payload": payload,
        },
    )

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


def test_routes_register_goal_endpoint_and_kickoff_stream():
    assert 'if parsed.path == "/api/goal"' in ROUTES_PY
    assert "return _handle_goal_command(handler, body)" in ROUTES_PY
    assert "goal_command_payload" in ROUTES_PY
    assert "kickoff_prompt" in ROUTES_PY
    assert "_start_chat_stream_for_session" in ROUTES_PY


def test_streaming_post_turn_goal_hook_surfaces_and_continues():
    assert "evaluate_goal_after_turn" in STREAMING_PY
    assert "put('goal'" in STREAMING_PY
    assert "decision.get('should_continue')" in STREAMING_PY
    assert "continuation_prompt" in STREAMING_PY
    assert "put('goal_continue'" in STREAMING_PY
    goal_idx = STREAMING_PY.find("evaluate_goal_after_turn")
    done_idx = STREAMING_PY.find("put('done'", goal_idx)
    assert goal_idx != -1 and done_idx != -1
    assert goal_idx < done_idx, (
        "goal status should be emitted before the terminal done payload"
    )


def test_streaming_goal_hook_emits_evaluating_state_before_judge():
    evaluating_idx = STREAMING_PY.find("'state': 'evaluating'")
    judge_idx = STREAMING_PY.find("_goal_decision = evaluate_goal_after_turn")
    done_idx = STREAMING_PY.find("put('done'", judge_idx)
    assert evaluating_idx != -1, (
        "goal hook should emit an evaluating state before judge round-trip"
    )
    assert judge_idx != -1 and done_idx != -1
    assert evaluating_idx < judge_idx < done_idx
    assert "Evaluating goal progress…" in STREAMING_PY
    assert (
        "'state': 'continuing' if decision.get('should_continue') else 'idle'"
        in STREAMING_PY
    )


def test_frontend_has_goal_slash_command_and_status_event_handler():
    assert "{name:'goal'" in COMMANDS_JS
    assert "subArgs:['status','pause','resume','clear']" in COMMANDS_JS
    assert "function cmdGoal" in COMMANDS_JS
    assert "api('/api/goal'" in COMMANDS_JS
    assert "stream_id" in COMMANDS_JS
    assert "goal'" in MESSAGES_JS
    assert "source.addEventListener('goal'" in MESSAGES_JS
    assert "source.addEventListener('goal_continue'" in MESSAGES_JS
    assert (
        "['steer','interrupt','queue','terminal','goal'].includes(_pc.name)"
        in MESSAGES_JS
    )
    assert "queueSessionMessage" in MESSAGES_JS


def test_frontend_goal_evaluating_state_uses_calm_composer_indicator():
    assert "const goalState=String(d.state||'').trim();" in MESSAGES_JS
    assert "const goalEvaluatingMessage='Evaluating goal progress…';" in MESSAGES_JS
    assert "if(goalState==='evaluating')" in MESSAGES_JS
    assert "setComposerStatus(goalEvaluatingMessage);" in MESSAGES_JS
    assert "return;" in MESSAGES_JS
