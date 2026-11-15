"""WebUI `/goal draft <text>` and `/goal show` against the installed Hermes Agent.

`/goal draft` delegates to Hermes' native `hermes_cli.goal_command` handler (the
one the CLI, TUI and Desktop use), so drafting, the empty-objective usage error
and the free-form fallback are Hermes behavior. These tests drive the real
native GoalManager on an isolated profile home and stub only the auxiliary
model calls (`draft_contract`, `_call_goal_judge_llm`).
"""

from pathlib import Path

import pytest

SESSION = "sid-goal-draft"
OBJECTIVE = "migrate auth from session cookies to JWT"


@pytest.fixture
def native(monkeypatch):
    goals = pytest.importorskip("hermes_cli.goals", reason="hermes-agent not installed")
    goal_command = pytest.importorskip("hermes_cli.goal_command", reason="hermes-agent without goal_command")
    monkeypatch.syspath_prepend(str(Path(goals.__file__).resolve().parents[1]))
    return goals, goal_command


@pytest.fixture
def webui_goals(native):
    from api import goals as webui_goals

    goals, _ = native
    webui_goals._DB_CACHE.clear()
    goals._DB_CACHE.clear()
    yield webui_goals
    for cache in (webui_goals._DB_CACHE, goals._DB_CACHE):
        for db in cache.values():
            close = getattr(db, "close", None)
            if close is not None:
                close()
        cache.clear()


@pytest.fixture
def profile_home(tmp_path):
    return tmp_path / "profile-a"


def _contract(goals):
    return goals.GoalContract(
        verification="pytest -q tests/auth passes",
        constraints="keep the /login response shape",
    )


def _state(webui_goals, home):
    return webui_goals.goal_state_snapshot(SESSION, profile_home=home)


def _stub_draft(monkeypatch, goals, result, calls):
    from hermes_constants import get_hermes_home

    def fake_draft(objective, **_kw):
        calls.append({"objective": objective, "home": Path(get_hermes_home()).resolve()})
        return result

    monkeypatch.setattr(goals, "draft_contract", fake_draft)


def test_draft_success_stores_contract_and_kicks_off(monkeypatch, native, webui_goals, profile_home):
    goals, _ = native
    calls = []
    _stub_draft(monkeypatch, goals, _contract(goals), calls)

    body = webui_goals.goal_command_payload(SESSION, f"draft {OBJECTIVE}", profile_home=profile_home)

    assert body["ok"] is True and body["action"] == "draft"
    assert body["message"].startswith(f"⊙ Goal set (20-turn budget): {OBJECTIVE}")
    assert "Drafted completion contract:" in body["message"]
    assert "- Verification: pytest -q tests/auth passes" in body["message"]
    assert "Tighten any field" not in body["message"]
    assert body["kickoff_prompt"] == OBJECTIVE
    assert body["toast_type"] == "info" and "toast_message" not in body
    state = _state(webui_goals, profile_home)
    assert state.goal == OBJECTIVE and state.status == "active"
    assert state.contract.verification == "pytest -q tests/auth passes"
    # The drafter (an auxiliary-model call) ran under the session's profile home.
    assert calls == [{"objective": OBJECTIVE, "home": profile_home.resolve()}]


def test_draft_keyword_is_case_insensitive(monkeypatch, native, webui_goals, profile_home):
    goals, _ = native
    calls = []
    _stub_draft(monkeypatch, goals, _contract(goals), calls)

    body = webui_goals.goal_command_payload(SESSION, f"DRAFT {OBJECTIVE}", profile_home=profile_home)

    assert body["action"] == "draft" and [c["objective"] for c in calls] == [OBJECTIVE]


def test_draft_failure_falls_back_exactly_like_native(monkeypatch, native, webui_goals, profile_home, tmp_path):
    goals, goal_command = native
    calls = []
    _stub_draft(monkeypatch, goals, None, calls)

    body = webui_goals.goal_command_payload(SESSION, f"draft {OBJECTIVE}", profile_home=profile_home)

    # Same handler, same input: the TUI/CLI output for a failed draft.
    native_mgr = webui_goals._manager("sid-native-reference", profile_home=tmp_path / "reference")
    expected = native_mgr._scoped(
        goal_command.dispatch_goal_command, native_mgr, f"draft {OBJECTIVE}", authorize_gate=lambda: None
    ).output
    assert body["ok"] is True and body["message"] == expected
    assert "Couldn't draft a contract (aux model unavailable)" in body["message"]
    assert body["toast_type"] == "warning"
    assert body["toast_message"].startswith("Couldn't draft a contract (aux model unavailable)")
    assert body["kickoff_prompt"] == OBJECTIVE
    state = _state(webui_goals, profile_home)
    assert state.goal == OBJECTIVE and state.status == "active" and not state.has_contract()


def test_draft_without_objective_is_usage_error_and_changes_nothing(
    monkeypatch, native, webui_goals, profile_home
):
    goals, _ = native
    calls = []
    _stub_draft(monkeypatch, goals, _contract(goals), calls)
    assert webui_goals.goal_command_payload(SESSION, "existing goal", profile_home=profile_home)["ok"]
    before = _state(webui_goals, profile_home).to_json()

    body = webui_goals.goal_command_payload(SESSION, "draft", profile_home=profile_home)

    assert body["ok"] is False and body["action"] == "draft"
    assert body["message"] == "Usage: /goal draft <objective in plain language>"
    # No error code: the route surfaces `error || message`, so the usage text is what the user sees.
    assert "error" not in body and "kickoff_prompt" not in body
    assert calls == []
    assert _state(webui_goals, profile_home).to_json() == before


def test_draft_while_agent_running_is_rejected_without_drafting(monkeypatch, native, webui_goals, profile_home):
    goals, _ = native
    calls = []
    _stub_draft(monkeypatch, goals, _contract(goals), calls)

    body = webui_goals.goal_command_payload(
        SESSION, f"draft {OBJECTIVE}", stream_running=True, profile_home=profile_home
    )

    assert body["ok"] is False and body["error"] == "agent_running"
    assert calls == [] and _state(webui_goals, profile_home) is None


def test_drafting_as_first_word_is_a_plain_goal(monkeypatch, native, webui_goals, profile_home):
    goals, _ = native
    calls = []
    _stub_draft(monkeypatch, goals, _contract(goals), calls)

    body = webui_goals.goal_command_payload(SESSION, "drafting docs", profile_home=profile_home)

    assert body["action"] == "set" and calls == []
    assert _state(webui_goals, profile_home).goal == "drafting docs"


def test_drafted_contract_reaches_judge_and_continuation(monkeypatch, native, webui_goals, profile_home):
    """The point of draft: WebUI's per-turn judge decides against the contract."""
    goals, _ = native
    _stub_draft(monkeypatch, goals, _contract(goals), [])
    webui_goals.goal_command_payload(SESSION, f"draft {OBJECTIVE}", profile_home=profile_home)
    prompts = []

    def fake_judge_llm(_call_llm, system_prompt, user_prompt, _timeout):
        prompts.append(user_prompt)
        return '{"verdict": "continue", "reason": "no test output shown"}'

    monkeypatch.setattr(goals, "_call_goal_judge_llm", fake_judge_llm)

    decision = webui_goals.evaluate_goal_after_turn(SESSION, "I think auth is done.", profile_home=profile_home)

    assert len(prompts) == 1
    assert "Completion contract (the authoritative definition of done):" in prompts[0]
    assert "pytest -q tests/auth passes" in prompts[0]
    assert decision["should_continue"] is True
    assert "Completion contract:" in decision["continuation_prompt"]


def test_show_without_goal_is_read_only(native, webui_goals, profile_home):
    body = webui_goals.goal_command_payload(SESSION, "show", profile_home=profile_home)

    assert body["ok"] is True and body["action"] == "show"
    assert body["message_key"] == "goal_status_none"
    assert "detail" not in body and "kickoff_prompt" not in body
    assert _state(webui_goals, profile_home) is None


def test_show_plain_goal_reports_missing_contract(native, webui_goals, profile_home):
    webui_goals.goal_command_payload(SESSION, "ship it", profile_home=profile_home)
    before = _state(webui_goals, profile_home).to_json()

    body = webui_goals.goal_command_payload(SESSION, "show", profile_home=profile_home)

    assert body["message_key"] == "goal_status_active"
    assert body["detail"].startswith("(no completion contract")
    assert "kickoff_prompt" not in body
    assert _state(webui_goals, profile_home).to_json() == before


def test_show_with_trailing_words_shows_contract_without_replacing_goal(
    monkeypatch, native, webui_goals, profile_home
):
    goals, _ = native
    _stub_draft(monkeypatch, goals, _contract(goals), [])
    webui_goals.goal_command_payload(SESSION, f"draft {OBJECTIVE}", profile_home=profile_home)
    before = _state(webui_goals, profile_home).to_json()

    body = webui_goals.goal_command_payload(SESSION, "show me the contract", profile_home=profile_home)

    assert body["action"] == "show" and "kickoff_prompt" not in body
    assert body["message"] == f"⊙ Goal (active, 0/20 turns): {OBJECTIVE}"
    assert "- Verification: pytest -q tests/auth passes" in body["detail"]
    assert "- Constraints: keep the /login response shape" in body["detail"]
    assert _state(webui_goals, profile_home).to_json() == before


def test_native_draft_hint_text_is_pinned(monkeypatch, native, webui_goals, tmp_path):
    """WebUI drops this exact native sentence; fail loudly if Hermes rewords it."""
    goals, goal_command = native
    _stub_draft(monkeypatch, goals, _contract(goals), [])
    mgr = webui_goals._manager("sid-hint", profile_home=tmp_path / "hint")

    output = mgr._scoped(
        goal_command.dispatch_goal_command, mgr, f"draft {OBJECTIVE}", authorize_gate=lambda: None
    ).output

    assert webui_goals._NATIVE_DRAFT_TIGHTEN_HINT in output


def test_draft_refuses_legacy_manager_without_changes(monkeypatch, native, webui_goals, tmp_path):
    goals, _ = native
    calls = []
    _stub_draft(monkeypatch, goals, _contract(goals), calls)
    legacy = webui_goals._LegacyProfileGoalManager(session_id=SESSION, profile_home=tmp_path / "legacy")

    body = webui_goals._draft_goal_payload(legacy, f"draft {OBJECTIVE}")

    assert body["ok"] is False and body["message"] == "/goal draft requires a newer Hermes Agent."
    assert calls == [] and legacy.state is None


def test_runtime_adapter_classifies_show_and_draft():
    from api import routes

    assert routes._runtime_adapter_goal_action("show") == "status"
    assert routes._runtime_adapter_goal_action("Show me") == "status"
    assert routes._runtime_adapter_goal_action("showcase the demo") == "set"
    assert routes._runtime_adapter_goal_action(f"draft {OBJECTIVE}") == "set"


class _FakeSession:
    session_id = SESSION
    profile = "default"
    model = "gpt-5.5"
    model_provider = "openai-codex"
    messages = []
    context_messages = []
    pending_user_message = None
    active_stream_id = None

    def __init__(self, workspace):
        self.workspace = workspace


def _patch_route(monkeypatch, tmp_path, profile_home):
    from api import profiles, routes

    resolved, started = [], []
    session = _FakeSession(str(tmp_path))

    def fake_workspace(workspace, **_kw):
        resolved.append(workspace)
        return tmp_path

    def fake_start(sess, **kwargs):
        started.append(kwargs)
        return {"stream_id": "goal-stream", "session_id": sess.session_id, "pending_started_at": 1.0}

    monkeypatch.setattr(profiles, "get_hermes_home_for_profile", lambda _name: profile_home)
    monkeypatch.setattr(routes, "get_session", lambda _sid: session)
    monkeypatch.setattr(routes, "resolve_trusted_workspace", fake_workspace)
    monkeypatch.setattr(routes, "webui_gateway_chat_enabled", lambda _cfg: False)
    monkeypatch.setattr(routes, "get_config", lambda: {})
    monkeypatch.setattr(
        routes, "_resolve_compatible_session_model_state", lambda model, provider, **_: (model, provider, False)
    )
    monkeypatch.setattr(routes, "_start_chat_stream_for_session", fake_start)
    monkeypatch.setattr(routes, "j", lambda _h, payload, status=200, **_kw: {"status": status, "payload": payload})
    return routes, resolved, started


def test_goal_route_draft_kicks_off_with_contract(monkeypatch, native, webui_goals, tmp_path, profile_home):
    goals, _ = native
    _stub_draft(monkeypatch, goals, _contract(goals), [])
    routes, resolved, started = _patch_route(monkeypatch, tmp_path, profile_home)

    result = routes._handle_goal_command(object(), {"session_id": SESSION, "args": f"draft {OBJECTIVE}"})

    assert result["status"] == 200
    assert result["payload"]["action"] == "draft" and result["payload"]["stream_id"] == "goal-stream"
    assert [s["msg"] for s in started] == [OBJECTIVE]
    assert _state(webui_goals, profile_home).contract.verification == "pytest -q tests/auth passes"


def test_goal_route_show_never_prepares_a_kickoff(monkeypatch, native, webui_goals, tmp_path, profile_home):
    routes, resolved, started = _patch_route(monkeypatch, tmp_path, profile_home)
    webui_goals.goal_command_payload(SESSION, "ship it", profile_home=profile_home)
    before = _state(webui_goals, profile_home).to_json()

    result = routes._handle_goal_command(object(), {"session_id": SESSION, "args": "show"})

    assert result["status"] == 200 and result["payload"]["action"] == "show"
    assert resolved == [] and started == []
    assert _state(webui_goals, profile_home).to_json() == before


def test_goal_route_empty_draft_is_400_with_usage_text(monkeypatch, native, webui_goals, tmp_path, profile_home):
    routes, resolved, started = _patch_route(monkeypatch, tmp_path, profile_home)

    result = routes._handle_goal_command(object(), {"session_id": SESSION, "args": "draft"})

    assert result["status"] == 400
    assert result["payload"]["message"] == "Usage: /goal draft <objective in plain language>"
    assert started == [] and _state(webui_goals, profile_home) is None
