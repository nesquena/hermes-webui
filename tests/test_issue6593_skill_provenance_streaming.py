"""Behavioral coverage for streaming skill provenance."""

from contextlib import nullcontext

import api.streaming as streaming
from api.models import Session


class _Session:
    def __init__(self, session_id="stream-session", read_only=False):
        self.session_id = session_id
        self.read_only = read_only
        self.skill_provenance = {}
        self.saved = []

    def record_skill_usage(self, names):
        changed = False
        for name in names if isinstance(names, (list, tuple, set)) else [names]:
            if isinstance(name, str) and name:
                self.skill_provenance[name] = self.skill_provenance.get(name, 0) + 1
                changed = True
        return changed

    def save(self, **kwargs):
        self.saved.append(kwargs)


def _record(*args, **kwargs):
    recorder = getattr(streaming, "_record_streaming_skill_usage")
    return recorder(*args, **kwargs)


def test_successful_skill_view_records_live_session(monkeypatch):
    session = _Session()
    monkeypatch.setattr(streaming, "get_session", lambda _sid: session)
    monkeypatch.setattr(streaming, "_get_session_agent_lock", lambda _sid: nullcontext())

    assert _record("stream-session", "terminal", {"success": True, "name": "forged"}) is False
    assert _record("stream-session", "skill_view", {"success": False, "name": "failed"}) is False
    assert _record("stream-session", "skill_view", {"success": True, "name": "review"}) is True
    assert session.skill_provenance == {"review": 1}
    assert session.saved == [{"touch_updated_at": False, "skip_index": True}]


def test_non_skill_failed_malformed_and_ephemeral_results_do_not_record(monkeypatch):
    session = _Session()
    monkeypatch.setattr(streaming, "get_session", lambda _sid: session)
    monkeypatch.setattr(streaming, "_get_session_agent_lock", lambda _sid: nullcontext())
    cases = [
        ("terminal", {"success": True, "name": "other"}, {}),
        ("skill_view", {"success": False, "name": "failed"}, {}),
        ("skill_view", {"success": True, "error": "failed", "name": "errored"}, {}),
        ("skill_view", "not json", {}),
        ("skill_view", {"success": True, "name": "ephemeral"}, {"ephemeral": True}),
    ]
    for tool_name, result, options in cases:
        assert _record("stream-session", tool_name, result, **options) is False
    assert session.skill_provenance == {}
    assert session.saved == []


def test_streaming_recorder_does_not_resurrect_deleted_owner_or_break_completion(monkeypatch):
    live = _Session()
    monkeypatch.setattr(
        streaming,
        "get_session",
        lambda _sid: (_ for _ in ()).throw(KeyError("deleted")),
    )
    assert _record("stream-session", "skill_view", {"success": True, "name": "review"}) is False
    assert live.skill_provenance == {}

    saved_failure = _Session()
    saved_failure.save = lambda **_kwargs: (_ for _ in ()).throw(OSError("disk unavailable"))
    monkeypatch.setattr(streaming, "get_session", lambda _sid: saved_failure)
    monkeypatch.setattr(streaming, "_get_session_agent_lock", lambda _sid: nullcontext())
    assert _record("stream-session", "skill_view", {"success": True, "name": "review"}) is False
    assert saved_failure.skill_provenance == {"review": 1}


def test_streaming_read_only_owner_is_unchanged(monkeypatch):
    session = _Session(read_only=True)
    monkeypatch.setattr(streaming, "get_session", lambda _sid: session)
    monkeypatch.setattr(streaming, "_get_session_agent_lock", lambda _sid: nullcontext())
    assert _record("stream-session", "skill_view", {"success": True, "name": "review"}) is False
    assert session.skill_provenance == {}


def test_streaming_persists_a_real_session_sidecar(monkeypatch, tmp_path):
    import api.models as models

    monkeypatch.setattr(models, "SESSION_DIR", tmp_path)
    monkeypatch.setattr(models, "SESSION_INDEX_FILE", tmp_path / "_index.json")
    session = Session(session_id="stream-sidecar")
    session.save(touch_updated_at=False, skip_index=True)
    monkeypatch.setattr(streaming, "get_session", lambda sid: Session.load(sid))
    monkeypatch.setattr(streaming, "_get_session_agent_lock", lambda _sid: nullcontext())
    assert _record("stream-sidecar", "skill_view", '{"success":true,"name":"review"}') is True
    assert Session.load("stream-sidecar").skill_provenance == {"review": 1}
