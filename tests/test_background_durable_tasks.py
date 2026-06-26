from __future__ import annotations

import json

import pytest


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


def test_durable_task_prompt_preview_is_bounded(tmp_path, monkeypatch):
    import api.background as bg

    monkeypatch.setattr(bg, "SESSION_DIR", tmp_path)
    bg._BACKGROUND_TASKS.clear()

    parent = "parent-session-preview"
    bg.track_background(parent, "bg-session", "stream-1", "task-1", "x" * 600)

    task = bg.list_durable_background_tasks(parent)[0]
    assert len(task["prompt_preview"]) < 540
    assert task["prompt_preview"].endswith("…(truncated)")


def test_durable_task_store_rejects_unsafe_parent_session_id(tmp_path, monkeypatch):
    import api.background as bg

    monkeypatch.setattr(bg, "SESSION_DIR", tmp_path)
    with pytest.raises(ValueError):
        bg.track_background("../bad", "bg", "stream", "task", "prompt")
