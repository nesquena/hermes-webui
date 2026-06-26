from __future__ import annotations

import json
from pathlib import Path

import pytest


def _prepare_isolated_session_store(tmp_path, monkeypatch):
    import api.background as bg
    import api.config as cfg
    import api.models as models

    monkeypatch.setattr(bg, "SESSION_DIR", tmp_path)
    monkeypatch.setattr(models, "SESSION_DIR", tmp_path)
    monkeypatch.setattr(models, "SESSION_INDEX_FILE", tmp_path / "_index.json")
    monkeypatch.setattr(cfg, "SESSION_DIR", tmp_path)
    monkeypatch.setattr(cfg, "SESSION_INDEX_FILE", tmp_path / "_index.json")
    bg._BACKGROUND_TASKS.clear()
    cfg.STREAMS.clear()
    cfg.ACTIVE_RUNS.clear()
    return bg, cfg, models


def test_track_and_complete_background_writes_durable_sidecar(tmp_path, monkeypatch):
    import api.background as bg

    monkeypatch.setattr(bg, "SESSION_DIR", tmp_path)
    bg._BACKGROUND_TASKS.clear()

    parent = "parent-session-durable"
    bg.track_background(parent, "bg-session", "stream-1", "task-1", "deep follow-up")

    sidecar = tmp_path / f"{parent}.background_tasks.json"
    assert sidecar.exists()
    raw = json.loads(sidecar.read_text(encoding="utf-8"))
    assert raw["schema_version"] == 1
    assert raw["parent_session_id"] == parent
    assert raw["tasks"][0]["status"] == "running"
    assert raw["tasks"][0]["prompt_preview"] == "deep follow-up"

    bg.complete_background(parent, "task-1", "deeper answer")
    durable = bg.list_durable_background_tasks(parent)
    assert len(durable) == 1
    assert durable[0]["task_id"] == "task-1"
    assert durable[0]["status"] == "done"
    assert durable[0]["answer"] == "deeper answer"
    assert durable[0]["completed_at"] is not None

    # Legacy polling still consumes the in-memory done result, but durable state
    # remains available for the forthcoming parent-transcript/card migration.
    assert bg.get_results(parent)[0]["answer"] == "deeper answer"
    assert bg.get_results(parent) == []
    assert bg.list_durable_background_tasks(parent)[0]["answer"] == "deeper answer"
    assert bg.get_durable_background_task(parent, "task-1")["answer"] == "deeper answer"
    assert bg.get_durable_background_task(parent, "missing") is None

    status = bg.durable_background_status(parent)
    assert status["ok"] is True
    assert status["session_id"] == parent
    assert len(status["tasks"]) == 1
    assert status["tasks"][0]["status"] == "done"


def test_durable_task_prompt_preview_is_bounded(tmp_path, monkeypatch):
    import api.background as bg

    monkeypatch.setattr(bg, "SESSION_DIR", tmp_path)
    bg._BACKGROUND_TASKS.clear()

    parent = "parent-session-preview"
    bg.track_background(parent, "bg-session", "stream-1", "task-1", "x" * 600)

    task = bg.list_durable_background_tasks(parent)[0]
    assert len(task["prompt_preview"]) < 540
    assert task["prompt_preview"].endswith("…(truncated)")


def test_background_tasks_route_is_registered():
    routes = Path("api/routes.py").read_text(encoding="utf-8")
    assert 'parsed.path == "/api/background/tasks"' in routes
    assert "durable_background_status" in routes
    assert 'status["results"] = get_results(sid)' in routes


def test_durable_task_store_rejects_unsafe_parent_session_id(tmp_path, monkeypatch):
    import api.background as bg

    monkeypatch.setattr(bg, "SESSION_DIR", tmp_path)
    with pytest.raises(ValueError):
        bg.track_background("../bad", "bg", "stream", "task", "prompt")


def test_background_parent_session_card_and_result_are_idempotent(tmp_path, monkeypatch):
    bg, _cfg, models = _prepare_isolated_session_store(tmp_path, monkeypatch)

    parent = models.Session(
        session_id="parent-session-card",
        workspace=str(tmp_path),
        messages=[
            {"role": "user", "content": "please do a deeper pass"},
            {"role": "assistant", "content": "Short answer first."},
        ],
    )
    parent.context_messages = list(parent.messages)
    parent.save()

    bg.track_background(parent.session_id, "bg-session", "stream-1", "task-card", "deep follow-up")

    reloaded = models.Session.load(parent.session_id)
    assert reloaded is not None
    assert [m.get("_message_id") for m in reloaded.messages if isinstance(m, dict)].count("bgcard_task-card") == 1
    card = next(m for m in reloaded.messages if m.get("_message_id") == "bgcard_task-card")
    assert card["_background_task"]["status"] == "running"
    assert bg.get_durable_background_task(parent.session_id, "task-card")["parent_append_status"] == "card_written"

    bg.complete_background(parent.session_id, "task-card", "deeper answer")
    # Repeated completion/status projection must update in place, not append duplicates.
    bg.complete_background(parent.session_id, "task-card", "deeper answer")

    reloaded = models.Session.load(parent.session_id)
    assert reloaded is not None
    message_ids = [m.get("_message_id") for m in reloaded.messages if isinstance(m, dict)]
    assert message_ids.count("bgcard_task-card") == 1
    assert message_ids.count("bgresult_task-card") == 1
    card = next(m for m in reloaded.messages if m.get("_message_id") == "bgcard_task-card")
    result = next(m for m in reloaded.messages if m.get("_message_id") == "bgresult_task-card")
    assert card["_background_task"]["status"] == "done"
    assert card["_background_task"]["result_message_id"] == "bgresult_task-card"
    assert result["_background_result"]["task_id"] == "task-card"
    assert "deeper answer" in result["content"]
    assert bg.get_durable_background_task(parent.session_id, "task-card")["parent_append_status"] == "result_written"


def test_background_parent_append_defers_while_parent_stream_is_live(tmp_path, monkeypatch):
    bg, cfg, models = _prepare_isolated_session_store(tmp_path, monkeypatch)

    parent = models.Session(
        session_id="parent-session-active",
        workspace=str(tmp_path),
        messages=[{"role": "user", "content": "keep chatting"}],
        active_stream_id="parent-live-stream",
    )
    parent.save()
    cfg.STREAMS["parent-live-stream"] = object()

    bg.track_background(parent.session_id, "bg-session", "stream-1", "task-live", "deep follow-up")
    reloaded = models.Session.load(parent.session_id)
    assert reloaded is not None
    assert [m for m in reloaded.messages if isinstance(m, dict) and m.get("_background_task")] == []
    assert bg.get_durable_background_task(parent.session_id, "task-live")["parent_append_status"] == "card_pending"

    bg.complete_background(parent.session_id, "task-live", "deeper answer")
    reloaded = models.Session.load(parent.session_id)
    assert reloaded is not None
    assert [m for m in reloaded.messages if isinstance(m, dict) and (m.get("_background_task") or m.get("_background_result"))] == []
    assert bg.get_durable_background_task(parent.session_id, "task-live")["parent_append_status"] == "result_pending"

    cfg.STREAMS.clear()
    reloaded.active_stream_id = None
    reloaded.save()
    assert bg.append_or_queue_background_parent_update(parent.session_id, "task-live", "done") == "result_written"
    reloaded = models.Session.load(parent.session_id)
    assert reloaded is not None
    message_ids = [m.get("_message_id") for m in reloaded.messages if isinstance(m, dict)]
    assert "bgcard_task-live" in message_ids
    assert "bgresult_task-live" in message_ids
