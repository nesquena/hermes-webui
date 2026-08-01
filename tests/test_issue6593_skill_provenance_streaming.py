from contextlib import nullcontext

import api.streaming as streaming


class _Session:
    def __init__(self):
        self.session_id = "stream-session"
        self.names = []
        self.saved = 0

    def record_server_skill_names(self, names):
        self.names.extend(names)
        return True

    def save(self, **kwargs):
        self.saved += 1


def test_streaming_completion_uses_server_tool_identity_before_parsing(monkeypatch):
    session = _Session()
    lock_calls = []
    monkeypatch.setattr(streaming, "get_session", lambda _session_id: session)
    monkeypatch.setattr(
        streaming,
        "_get_session_agent_lock",
        lambda session_id: lock_calls.append(session_id) or nullcontext(),
    )

    assert streaming._record_streaming_skill_provenance(
        session,
        "stream-session",
        "terminal",
        '{"success": true, "name": "forged"}',
    ) is False
    assert streaming._record_streaming_skill_provenance(
        session,
        "stream-session",
        "skill_view",
        '{"success": false, "name": "failed"}',
    ) is False
    assert streaming._record_streaming_skill_provenance(
        session,
        "stream-session",
        "skill_view",
        '{"success": true, "name": "review"}',
    ) is True

    assert session.names == ["review"]
    assert session.saved == 1
    assert lock_calls == ["stream-session"]


def test_streaming_completion_does_not_resurrect_deleted_session(monkeypatch):
    session = _Session()
    monkeypatch.setattr(
        streaming,
        "get_session",
        lambda session_id: (_ for _ in ()).throw(KeyError(session_id)),
    )

    assert streaming._record_streaming_skill_provenance(
        session,
        "stream-session",
        "skill_view",
        '{"success": true, "name": "review"}',
    ) is False
    assert session.names == []
    assert session.saved == 0


def test_streaming_save_failure_does_not_escape_tool_completion(monkeypatch):
    session = _Session()
    session.save = lambda **kwargs: (_ for _ in ()).throw(OSError("disk unavailable"))
    monkeypatch.setattr(streaming, "get_session", lambda _session_id: session)
    monkeypatch.setattr(streaming, "_get_session_agent_lock", lambda _session_id: nullcontext())
    assert streaming._record_streaming_skill_provenance(
        session,
        "stream-session",
        "skill_view",
        '{"success": true, "name": "review"}',
    ) is False
    assert session.names == ["review"]
