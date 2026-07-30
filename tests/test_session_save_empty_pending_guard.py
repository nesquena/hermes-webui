"""Regression coverage for empty active/pending session save writebacks."""

import json

import api.config as config
import api.models as models
from api.models import Session


def test_empty_active_pending_save_cannot_overwrite_existing_messages(tmp_path, monkeypatch):
    session_dir = tmp_path / "sessions"
    session_dir.mkdir()
    index_file = session_dir / "_index.json"
    monkeypatch.setattr(models, "SESSION_DIR", session_dir)
    monkeypatch.setattr(models, "SESSION_INDEX_FILE", index_file)
    monkeypatch.setattr(config, "SESSION_INDEX_FILE", index_file, raising=False)
    models.SESSIONS.clear()

    sid = "pending_overwrite_guard"
    existing = Session(
        session_id=sid,
        messages=[
            {"role": "user", "content": "prompt"},
            {"role": "assistant", "content": "answer"},
        ],
    )
    existing.save()

    stale = Session(
        session_id=sid,
        messages=[],
        active_stream_id="stale-stream",
        pending_user_message="prompt",
        pending_started_at=123.0,
    )
    stale.save()

    persisted = json.loads((session_dir / f"{sid}.json").read_text(encoding="utf-8"))
    assert [m["content"] for m in persisted["messages"]] == ["prompt", "answer"]
    assert persisted["message_count"] == 2

    index = json.loads(index_file.read_text(encoding="utf-8"))
    indexed = next(row for row in index if row["session_id"] == sid)
    assert indexed["message_count"] == 2
