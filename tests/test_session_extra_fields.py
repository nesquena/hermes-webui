"""Regression tests for persisted Session extras.

Session.save() writes non-core fields back to JSON so behavior flags can
survive reloads. Loading must round-trip those extras instead of silently
dropping them.
"""

import api.models as models


def test_session_load_preserves_saved_extra_fields(tmp_path, monkeypatch):
    session_dir = tmp_path / "sessions"
    session_dir.mkdir()
    monkeypatch.setattr(models, "SESSION_DIR", session_dir)
    monkeypatch.setattr(models, "SESSION_INDEX_FILE", session_dir / "_index.json")
    models.SESSIONS.clear()

    sid = "20260427_extra_fields"
    session = models.Session(
        session_id=sid,
        title="Renamed Title",
        workspace=str(tmp_path),
        model="gpt-5.4",
        messages=[{"role": "user", "content": "hello", "timestamp": 1}],
    )
    session.user_renamed_title = True
    session.save()
    models.SESSIONS.clear()

    loaded = models.Session.load(sid)
    assert loaded is not None
    assert loaded.user_renamed_title is True
