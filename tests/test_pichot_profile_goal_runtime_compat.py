"""Regression coverage for the profile-scoped WebUI goal bridge."""

from pathlib import Path

from api import goals


class _MemoryMetaDB:
    def __init__(self):
        self.values: dict[str, str] = {}

    def get_meta(self, key: str):
        return self.values.get(key)

    def set_meta(self, key: str, value: str):
        self.values[key] = value


def _manager(monkeypatch, tmp_path: Path, *, db=None):
    db = db or _MemoryMetaDB()
    monkeypatch.setattr(goals, "_profile_db", lambda _profile_home: db)
    manager = goals._ProfileGoalManager(
        "profile-goal-runtime-compat",
        profile_home=tmp_path / "profile-home",
        default_max_turns=20,
    )
    return manager, db


def test_profile_goal_manager_accepts_current_five_value_judge_contract(monkeypatch, tmp_path):
    manager, _db = _manager(monkeypatch, tmp_path)
    manager.set("Finish the approved implementation")
    seen = {}

    def _judge(*_args, **kwargs):
        seen.update(kwargs)
        return "continue", "work remains", False, None, False

    monkeypatch.setattr(
        goals,
        "judge_goal",
        _judge,
    )

    decision = manager.evaluate_after_turn(
        "Implemented the first lot",
        background_processes=[{"session_id": "build-1", "pid": 123, "status": "running"}],
    )

    assert decision["status"] == "active"
    assert decision["should_continue"] is True
    assert decision["verdict"] == "continue"
    assert decision["reason"] == "work remains"
    assert manager.state is not None
    assert manager.state.turns_used == 1
    assert manager.state.consecutive_parse_failures == 0
    assert manager.state.consecutive_transport_failures == 0
    assert seen["background_processes"] == [
        {"session_id": "build-1", "pid": 123, "status": "running"}
    ]


def test_profile_goal_manager_parks_on_current_wait_directive(monkeypatch, tmp_path):
    manager, _db = _manager(monkeypatch, tmp_path)
    manager.set("Wait safely for the asynchronous deployment")
    monkeypatch.setattr(
        goals,
        "judge_goal",
        lambda *_args, **_kwargs: (
            "wait",
            "deployment is still running",
            False,
            {"seconds": 30},
            False,
        ),
    )

    decision = manager.evaluate_after_turn("The deployment is still running")

    assert decision["status"] == "active"
    assert decision["should_continue"] is False
    assert decision["verdict"] == "wait"
    assert manager.state is not None
    assert manager.state.waiting_until > manager.state.waiting_since > 0
    assert manager.state.waiting_reason == "deployment is still running"

    turns_used = manager.state.turns_used
    parked = manager.evaluate_after_turn("Do not repoke while still waiting")
    assert parked["verdict"] == "waiting"
    assert parked["should_continue"] is False
    assert manager.state.turns_used == turns_used


def test_profile_goal_pause_and_resume_clear_wait_barrier(monkeypatch, tmp_path):
    manager, _db = _manager(monkeypatch, tmp_path)
    manager.set("Wait safely")
    manager.wait_for_seconds(30, reason="deployment running")

    paused = manager.pause("operator pause")
    assert paused is not None
    assert paused.status == "paused"
    assert paused.waiting_until == 0.0
    assert paused.waiting_reason is None

    paused.waiting_until = 9999999999.0
    paused.waiting_reason = "stale wait"
    resumed = manager.resume()
    assert resumed is not None
    assert resumed.status == "active"
    assert resumed.waiting_until == 0.0
    assert resumed.waiting_reason is None


def test_profile_goal_manager_pauses_after_repeated_parse_failures(monkeypatch, tmp_path):
    manager, _db = _manager(monkeypatch, tmp_path)
    manager.set("Finish safely")
    monkeypatch.setattr(
        goals,
        "judge_goal",
        lambda *_args, **_kwargs: ("continue", "judge reply was not JSON", True, None, False),
    )

    decisions = [manager.evaluate_after_turn("work continues") for _ in range(3)]

    assert [item["status"] for item in decisions] == ["active", "active", "paused"]
    assert decisions[-1]["should_continue"] is False
    assert manager.state is not None
    assert manager.state.consecutive_parse_failures == 3
    assert "unparseable" in (manager.state.paused_reason or "")


def test_profile_goal_manager_pauses_after_repeated_transport_failures(monkeypatch, tmp_path):
    manager, _db = _manager(monkeypatch, tmp_path)
    manager.set("Finish safely")
    monkeypatch.setattr(
        goals,
        "judge_goal",
        lambda *_args, **_kwargs: ("continue", "judge API unavailable", False, None, True),
    )

    decisions = [manager.evaluate_after_turn("work continues") for _ in range(5)]

    assert [item["status"] for item in decisions] == [
        "active",
        "active",
        "active",
        "active",
        "paused",
    ]
    assert decisions[-1]["should_continue"] is False
    assert manager.state is not None
    assert manager.state.consecutive_transport_failures == 5
    assert "unreachable" in (manager.state.paused_reason or "")


def test_profile_goal_manager_persists_inline_completion_contract(monkeypatch, tmp_path):
    manager, db = _manager(monkeypatch, tmp_path)
    state = manager.set(
        "/validation\n"
        "outcome: livrer le plan approuvé jusqu’au live vérifié.\n"
        "verify: toutes les validations et preuves live sont terminées.\n"
        "constraints: respecter le périmètre approuvé et les gates.\n"
        "stop when: un blocage Direction réel exige une décision."
    )

    assert state.goal == "/validation"
    assert state.contract.outcome == "livrer le plan approuvé jusqu’au live vérifié."
    assert state.contract.verification == "toutes les validations et preuves live sont terminées."
    assert state.contract.constraints == "respecter le périmètre approuvé et les gates."
    assert state.contract.stop_when == "un blocage Direction réel exige une décision."

    reloaded, _db = _manager(monkeypatch, tmp_path, db=db)
    assert reloaded.state is not None
    assert reloaded.state.goal == "/validation"
    assert reloaded.state.contract.to_dict() == state.contract.to_dict()

    continuation = reloaded.next_continuation_prompt()
    assert continuation is not None
    assert "Completion contract" in continuation
    assert "toutes les validations et preuves live sont terminées" in continuation
    assert "un blocage Direction réel exige une décision" in continuation


def test_webui_goal_hook_gathers_live_background_processes(monkeypatch, tmp_path):
    manager, _db = _manager(monkeypatch, tmp_path)
    manager.set("Finish the approved implementation")
    seen = {}

    def _evaluate(last_response, *, user_initiated=True, background_processes=None):
        seen["last_response"] = last_response
        seen["user_initiated"] = user_initiated
        seen["background_processes"] = background_processes
        return {"status": "active", "should_continue": True}

    monkeypatch.setattr(manager, "evaluate_after_turn", _evaluate)
    monkeypatch.setattr(goals, "_manager", lambda *_args, **_kwargs: manager)
    monkeypatch.setattr(
        goals,
        "gather_background_processes",
        lambda: [{"session_id": "deploy-7", "status": "running"}],
    )

    decision = goals.evaluate_goal_after_turn(
        "sid-validation",
        "deployment started",
        profile_home=tmp_path / "profile-home",
    )

    assert decision["should_continue"] is True
    assert seen["last_response"] == "deployment started"
    assert seen["background_processes"] == [
        {"session_id": "deploy-7", "status": "running"}
    ]
