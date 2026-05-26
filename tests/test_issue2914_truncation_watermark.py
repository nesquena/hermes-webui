"""Regression tests for #2914 state.db tail replay after undo/retry/edit."""
from __future__ import annotations


def _msg(role: str, content: str, ts: float, mid: str) -> dict:
    return {"id": mid, "role": role, "content": content, "timestamp": ts}


def test_reconciled_messages_skip_state_tail_after_sidecar_truncation():
    from api.models import Session, reconciled_state_db_messages_for_session

    sidecar = [
        _msg("user", "first", 1.0, "sidecar-u1"),
        _msg("assistant", "reply first", 2.0, "sidecar-a1"),
    ]
    state_db = [
        _msg("user", "first", 1.0, "state-u1"),
        _msg("assistant", "reply first", 2.0, "state-a1"),
        _msg("user", "second", 3.0, "state-u2"),
        _msg("assistant", "reply second", 4.0, "state-a2"),
    ]
    session = Session(
        session_id="issue2914",
        messages=sidecar,
        truncation_watermark=2.0,
    )

    merged = reconciled_state_db_messages_for_session(session, state_messages=state_db)

    assert [m["content"] for m in merged] == ["first", "reply first"]


def test_empty_sidecar_truncation_watermark_blocks_state_replay():
    from api.models import Session, reconciled_state_db_messages_for_session

    state_db = [
        _msg("user", "only prompt", 1.0, "state-u1"),
        _msg("assistant", "only reply", 2.0, "state-a1"),
    ]
    session = Session(
        session_id="issue2914empty",
        messages=[],
        truncation_watermark=0.0,
    )

    assert reconciled_state_db_messages_for_session(session, state_messages=state_db) == []


def test_undo_persists_truncation_watermark_at_new_tail(monkeypatch, tmp_path):
    import api.models as models
    from api.models import Session
    from api.session_ops import undo_last

    session_dir = tmp_path / "sessions"
    session_dir.mkdir(parents=True)
    monkeypatch.setattr(models, "SESSION_DIR", session_dir)
    monkeypatch.setattr(models, "SESSION_INDEX_FILE", session_dir / "_index.json")
    models.SESSIONS.clear()

    session = Session(
        session_id="issue2914undo",
        messages=[
            _msg("user", "first", 1.0, "u1"),
            _msg("assistant", "reply first", 2.0, "a1"),
            _msg("user", "second", 3.0, "u2"),
            _msg("assistant", "reply second", 4.0, "a2"),
        ],
    )
    session.save()

    undo_last("issue2914undo")

    loaded = Session.load("issue2914undo")
    assert loaded is not None
    assert [m["content"] for m in loaded.messages] == ["first", "reply first"]
    assert loaded.truncation_watermark == 2.0
