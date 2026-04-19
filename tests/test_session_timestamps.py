"""Regression tests for session message timestamp normalization."""

import collections
import json
import pathlib

from api import models
from api.models import Session, normalize_message_timestamps


def test_normalize_message_timestamps_spreads_uniform_history():
    msgs = [
        {"role": "user", "content": "hi", "timestamp": 111},
        {"role": "assistant", "content": "hello", "timestamp": 111},
        {"role": "user", "content": "again", "timestamp": 111},
    ]

    changed = normalize_message_timestamps(msgs, anchor_ts=120)

    assert changed is True
    ts = [m["timestamp"] for m in msgs]
    assert ts == [118, 119, 120]
    assert [m["_ts"] for m in msgs] == ts


def test_session_load_repairs_uniform_message_timestamps(tmp_path, monkeypatch):
    sessions_dir = tmp_path / "sessions"
    sessions_dir.mkdir(parents=True, exist_ok=True)
    index_file = tmp_path / "_session_index.json"
    sid = "tsfix_abc123"
    now = 1776138157

    session_payload = {
        "session_id": sid,
        "title": "Timestamp test",
        "workspace": "/tmp",
        "model": "test-model",
        "created_at": now,
        "updated_at": now,
        "pinned": False,
        "archived": False,
        "project_id": None,
        "profile": "default",
        "input_tokens": 0,
        "output_tokens": 0,
        "estimated_cost": None,
        "personality": None,
        "messages": [
            {"role": "user", "content": "one", "timestamp": now},
            {"role": "assistant", "content": "two", "timestamp": now},
            {"role": "user", "content": "three", "timestamp": now},
        ],
        "tool_calls": [],
    }
    (sessions_dir / f"{sid}.json").write_text(json.dumps(session_payload), encoding="utf-8")

    monkeypatch.setattr(models, "SESSION_DIR", sessions_dir)
    monkeypatch.setattr(models, "SESSION_INDEX_FILE", index_file)
    monkeypatch.setattr(models, "SESSIONS", collections.OrderedDict())

    loaded = Session.load(sid, persist=True)
    assert loaded is not None
    ts = [m["timestamp"] for m in loaded.messages]
    assert ts == [now - 2, now - 1, now]
    assert [m["_ts"] for m in loaded.messages] == ts

    reloaded = json.loads((sessions_dir / f"{sid}.json").read_text(encoding="utf-8"))
    persisted_ts = [m["timestamp"] for m in reloaded["messages"]]
    assert persisted_ts == [now - 2, now - 1, now]
