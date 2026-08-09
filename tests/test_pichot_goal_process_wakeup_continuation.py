"""Vertical regression for goal continuation after a process wakeup."""

from pathlib import Path

from api import goals, routes, runtime_adapter


class _MemoryMetaDB:
    def __init__(self):
        self.values: dict[str, str] = {}

    def get_meta(self, key: str):
        return self.values.get(key)

    def set_meta(self, key: str, value: str):
        self.values[key] = value


class _Session:
    session_id = "sid-validation-wakeup"
    profile = "default"


def test_process_wakeup_reenters_goal_chain_after_wait(
    monkeypatch,
    tmp_path: Path,
):
    captured = {}

    def _start_stream(*_args, **kwargs):
        captured.update(kwargs)
        return {"stream_id": "stream-wakeup", "_status": 200}

    monkeypatch.setattr(runtime_adapter, "runtime_adapter_enabled", lambda: False)
    monkeypatch.setattr(runtime_adapter, "runtime_adapter_runner_enabled", lambda: False)
    monkeypatch.setattr(routes, "_start_chat_stream_for_session", _start_stream)

    routes._start_run(
        _Session(),
        msg="background deployment completed",
        attachments=[],
        workspace=str(tmp_path),
        model="model-id",
        model_provider="provider-id",
        normalized_model=False,
        source="process_wakeup",
        route="start_session_turn",
    )

    assert captured["source"] == "process_wakeup"
    assert captured["goal_related"] is True

    db = _MemoryMetaDB()
    monkeypatch.setattr(goals, "_profile_db", lambda _profile_home: db)
    manager = goals._ProfileGoalManager(
        "sid-validation-wakeup",
        profile_home=tmp_path / "profile-home",
        default_max_turns=20,
    )
    manager.set("/validation")
    waiting = {"active": True}
    monkeypatch.setattr(goals, "_session_waiting", lambda _session_id: waiting["active"])
    monkeypatch.setattr(
        goals,
        "judge_goal",
        lambda *_args, **_kwargs: (
            "wait",
            "deployment running",
            False,
            {"session_id": "deploy-session"},
            False,
        ),
    )
    parked = manager.evaluate_after_turn("deployment running")
    assert parked["verdict"] == "wait"
    assert parked["should_continue"] is False

    waiting["active"] = False
    monkeypatch.setattr(
        goals,
        "judge_goal",
        lambda *_args, **_kwargs: ("continue", "live checks remain", False, None, False),
    )
    resumed = manager.evaluate_after_turn("background deployment completed")

    assert resumed["status"] == "active"
    assert resumed["verdict"] == "continue"
    assert resumed["should_continue"] is True
    assert resumed["continuation_prompt"]
    assert manager.state is not None
    assert manager.state.waiting_on_session is None