"""Regression: stale stream cleanup must not discard pending user turns.

A server restart drops the in-memory STREAMS table. Browser reload then calls
get_session(), which clears stale active_stream_id state. For long conversations
that already have messages, the pending_user_message can be the only durable copy
of the user turn that was submitted just before the restart.
"""

import api.config as config
import api.models as models
from api.models import Session, get_session


def test_stale_stream_cleanup_recovers_pending_turn_on_non_empty_session(
    tmp_path, monkeypatch
):
    session_dir = tmp_path / "sessions"
    session_dir.mkdir()
    monkeypatch.setattr(models, "SESSION_DIR", session_dir)
    monkeypatch.setattr(models, "SESSION_INDEX_FILE", session_dir / "_index.json")
    models.SESSIONS.clear()
    config.STREAMS.clear()

    s = Session(
        session_id="stale_stream_nonempty",
        title="Existing long chat",
        messages=[
            {"role": "user", "content": "previous prompt", "timestamp": 100},
            {"role": "assistant", "content": "previous answer", "timestamp": 101},
        ],
    )
    s.active_stream_id = "dead_stream"
    s.pending_user_message = "new prompt that must survive restart"
    s.pending_attachments = [{"name": "note.txt", "path": "/tmp/note.txt"}]
    s.pending_started_at = 123
    s.save()

    recovered = get_session("stale_stream_nonempty")

    assert recovered.active_stream_id is None
    assert recovered.pending_user_message is None
    assert any(
        msg.get("role") == "user"
        and msg.get("content") == "new prompt that must survive restart"
        and msg.get("_recovered") is True
        for msg in recovered.messages
    )
    assert any(
        msg.get("role") == "assistant" and msg.get("_error") is True
        for msg in recovered.messages
    )
