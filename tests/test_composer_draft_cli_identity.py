"""A draft save never re-saves a CLI chat as WebUI-native.

tests/test_session_effort_master_compose.py::test_draft_lock_held_reload_carries_cli_source_identity
protects this for a draft save that reloads the session under its agent lock and publishes the reloaded
object. The fast draft path writes only the draft sidecar: it neither takes that lock nor reloads or
publishes the session, so that test's mechanism assertions ("re-resolved under the lock") cannot hold
here. This test checks the property itself on the fast path, including the eviction that test uses.
"""
from __future__ import annotations

import json

import api.models as models
from api.models import SESSIONS, Session
from tests.test_session_effort_master_compose import _isolate_session_store, _post

IDENTITY = {
    "is_cli_session": True, "source_tag": "hermes-cli", "raw_source": "hermes-cli",
    "session_source": "cli", "source_label": "Hermes CLI",
}


def _cli_session(tmp_path, sid):
    Session(
        session_id=sid, title="cli title", workspace=str(tmp_path),
        messages=[{"role": "user", "content": "hi"}, {"role": "assistant", "content": "hello"}],
    ).save()
    resident = Session.load(sid)
    assert resident is not None
    for key, value in IDENTITY.items():
        setattr(resident, key, value)  # _get_or_materialize_session's in-memory stamp
    SESSIONS[sid] = resident
    return resident


def test_draft_save_keeps_resident_cli_identity_and_leaves_session_file_alone(tmp_path, monkeypatch):
    _isolate_session_store(tmp_path, monkeypatch)
    sid = "draft-cli-identity-8402"
    resident = _cli_session(tmp_path, sid)
    session_file = models.SESSION_DIR / f"{sid}.json"
    before = session_file.read_bytes()

    captured = _post(monkeypatch, "/api/session/draft", {"session_id": sid, "text": "new draft"})

    assert captured["status"] == 200, captured
    assert session_file.read_bytes() == before, "the draft save must not rewrite the session file"
    assert SESSIONS.get(sid) is resident
    for key, value in IDENTITY.items():
        assert getattr(SESSIONS[sid], key) == value, key
    resident.save(touch_updated_at=False)  # the next whole-session save keeps the identity
    on_disk = json.loads(session_file.read_text())
    for key, value in IDENTITY.items():
        assert on_disk.get(key) == value, key


def test_draft_save_after_eviction_publishes_no_webui_native_object(tmp_path, monkeypatch):
    _isolate_session_store(tmp_path, monkeypatch)
    sid = "draft-cli-evicted-8402"
    _cli_session(tmp_path, sid)
    session_file = models.SESSION_DIR / f"{sid}.json"
    before = session_file.read_bytes()
    SESSIONS.pop(sid, None)  # count-cap eviction, as in that test

    captured = _post(monkeypatch, "/api/session/draft", {"session_id": sid, "text": "new draft"})

    assert captured["status"] == 200, captured
    assert session_file.read_bytes() == before
    published = SESSIONS.get(sid)
    if published is not None:  # whatever the draft path published must not be a WebUI-native copy
        for key, value in IDENTITY.items():
            assert getattr(published, key, None) == value, key
