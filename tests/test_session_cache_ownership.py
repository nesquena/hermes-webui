import json
from io import BytesIO
from types import SimpleNamespace

import api.config as config
import api.models as models
from api.models import Session, get_session


def test_get_session_evicts_cached_object_with_wrong_session_id(tmp_path, monkeypatch):
    session_dir = tmp_path / "sessions"
    session_dir.mkdir()
    monkeypatch.setattr(models, "SESSION_DIR", session_dir)
    monkeypatch.setattr(models, "SESSION_INDEX_FILE", session_dir / "_index.json")
    monkeypatch.setattr(config, "SESSION_INDEX_FILE", session_dir / "_index.json", raising=False)
    models.SESSIONS.clear()

    requested = Session(
        session_id="requested-tip",
        title="Requested Tip",
        messages=[{"role": "user", "content": "correct transcript"}],
    )
    requested.save()
    wrong = Session(
        session_id="older-segment",
        title="Older Segment",
        messages=[{"role": "user", "content": "wrong transcript"}],
    )

    models.SESSIONS["requested-tip"] = wrong

    loaded = get_session("requested-tip")

    assert loaded.session_id == "requested-tip"
    assert loaded.title == "Requested Tip"
    assert loaded.messages == [{"role": "user", "content": "correct transcript"}]
    assert models.SESSIONS["requested-tip"] is loaded

    models.SESSIONS.clear()


def test_get_session_metadata_only_evicts_cached_object_with_wrong_session_id(tmp_path, monkeypatch):
    session_dir = tmp_path / "sessions"
    session_dir.mkdir()
    monkeypatch.setattr(models, "SESSION_DIR", session_dir)
    monkeypatch.setattr(models, "SESSION_INDEX_FILE", session_dir / "_index.json")
    monkeypatch.setattr(config, "SESSION_INDEX_FILE", session_dir / "_index.json", raising=False)
    models.SESSIONS.clear()

    requested = Session(
        session_id="requested-tip",
        title="Requested Tip",
        messages=[{"role": "user", "content": "correct transcript"}],
    )
    requested.save()
    models.SESSIONS["requested-tip"] = Session(session_id="older-segment", title="Older Segment")

    loaded = get_session("requested-tip", metadata_only=True)

    assert loaded.session_id == "requested-tip"
    assert loaded.title == "Requested Tip"
    assert models.SESSIONS.get("requested-tip") is None

    models.SESSIONS.clear()


def test_compression_cache_migration_never_moves_unverified_cached_object_to_new_sid():
    streaming_src = open("api/streaming.py", encoding="utf-8").read()

    assert "SESSIONS[new_sid] = SESSIONS.pop(old_sid)" not in streaming_src
    assert "cached_old_session is not s" in streaming_src
    assert "SESSIONS[new_sid] = s" in streaming_src


def test_cached_agent_session_identity_matches_requested_sid():
    from api.streaming import _cached_agent_matches_session, _cached_agent_session_identity

    matching = SimpleNamespace(session_id="requested")
    mismatched = SimpleNamespace(session_id="other")
    legacy = SimpleNamespace()
    db_owned = SimpleNamespace(_session_db=SimpleNamespace(session_id="requested"))

    assert _cached_agent_session_identity(matching) == "requested"
    assert _cached_agent_matches_session(matching, "requested") is True
    assert _cached_agent_matches_session(mismatched, "requested") is False
    assert _cached_agent_matches_session(db_owned, "requested") is True
    assert _cached_agent_matches_session(legacy, "requested") is True


def test_handle_chat_steer_ignores_mismatched_session_cache_without_stream_agent(monkeypatch):
    import api.streaming as streaming
    from api.streaming import _handle_chat_steer
    import queue

    class Handler:
        headers = {}

        def __init__(self):
            self.status = None
            self.response_headers = []
            self.wfile = BytesIO()

        def send_response(self, status):
            self.status = status

        def send_header(self, key, value):
            self.response_headers.append((key, value))

        def end_headers(self):
            pass

    wrong_agent = SimpleNamespace(session_id="other-session", steer=lambda _text: True)
    closed_entries = []
    monkeypatch.setattr(
        streaming,
        "_close_cached_agent_entry_at_session_boundary",
        lambda session_id, entry: closed_entries.append((session_id, entry)),
    )
    config.SESSION_AGENT_CACHE.clear()
    config.SESSION_AGENT_CACHE["requested"] = (wrong_agent, "sig")
    stream_id = "requested-stream"
    with config.STREAMS_LOCK:
        config.STREAMS[stream_id] = queue.Queue()
        config.AGENT_INSTANCES.pop(stream_id, None)
    monkeypatch.setattr(
        streaming,
        "get_session",
        lambda _sid: SimpleNamespace(active_stream_id=stream_id),
    )
    handler = Handler()

    try:
        _handle_chat_steer(handler, {"session_id": "requested", "text": "please steer"})
    finally:
        with config.STREAMS_LOCK:
            config.STREAMS.pop(stream_id, None)
            config.AGENT_INSTANCES.pop(stream_id, None)

    payload = json.loads(handler.wfile.getvalue().decode("utf-8"))
    assert handler.status == 200
    assert payload == {"accepted": False, "fallback": "no_cached_agent", "stream_id": None}
    assert config.SESSION_AGENT_CACHE["requested"] == (wrong_agent, "sig")
    assert closed_entries == []
    config.SESSION_AGENT_CACHE.clear()
