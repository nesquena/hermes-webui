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


def test_live_rotation_identity_matches_only_its_immediate_parent():
    from api.streaming import SESSIONS, _agent_matches_live_rotation_session

    session_db = SimpleNamespace(
        get_session=lambda sid: {
            "id": sid,
            "parent_session_id": "compression-origin",
        }
    )
    rotating = SimpleNamespace(
        session_id="compression-continuation",
        _session_db=session_db,
    )

    assert _agent_matches_live_rotation_session(rotating, "compression-continuation") is True
    assert _agent_matches_live_rotation_session(rotating, "compression-origin") is False
    assert _agent_matches_live_rotation_session(rotating, "unrelated-session") is False

    rotating._parent_session_id = "fork-origin"
    rotating._live_rotation_parent_session_id = "compression-origin"
    assert _agent_matches_live_rotation_session(rotating, "compression-origin") is True
    assert _agent_matches_live_rotation_session(rotating, "fork-origin") is False
    assert _agent_matches_live_rotation_session(rotating, "unrelated-session") is False

    # The Agent owns a SessionDB handle, but its durable current-session row can
    # lag the live rotation projection. Routing therefore trusts only SESSIONS.
    rotating._session_db.get_session = lambda _sid: {
        "id": "compression-continuation",
        "parent_session_id": "unrelated-session",
    }
    SESSIONS["compression-continuation"] = SimpleNamespace(
        session_id="compression-continuation",
        parent_session_id="compression-origin",
        pre_compression_snapshot=False,
    )
    try:
        assert _agent_matches_live_rotation_session(rotating, "compression-continuation") is True
        assert _agent_matches_live_rotation_session(rotating, "compression-origin") is True
        assert _agent_matches_live_rotation_session(rotating, "unrelated-session") is False
    finally:
        SESSIONS.pop("compression-continuation", None)


def test_identityless_agent_never_matches_global_live_rotation_lookup():
    from api.streaming import _agent_matches_live_rotation_session

    identityless = SimpleNamespace(steer=lambda _text: True)

    assert _agent_matches_live_rotation_session(identityless, "requested-session") is False


def test_publish_live_rotation_identity_updates_only_a_registered_live_agent():
    import queue
    import api.streaming as streaming

    stream_id = "stream-live-rotation"
    agent = SimpleNamespace(session_id="compression-continuation")
    streaming.STREAMS[stream_id] = queue.Queue()
    streaming.AGENT_INSTANCES[stream_id] = agent
    config.ACTIVE_RUNS[stream_id] = {
        "stream_id": stream_id,
        "session_id": "compression-origin",
        "phase": "running",
    }
    try:
        assert streaming._publish_live_rotation_identity(
            agent,
            stream_id=stream_id,
            old_session_id="compression-origin",
            new_session_id="compression-continuation",
        ) is True
        assert agent._live_rotation_parent_session_id == "compression-origin"
        assert config.ACTIVE_RUNS[stream_id]["session_id"] == "compression-continuation"

        streaming.STREAMS.pop(stream_id)
        assert streaming._publish_live_rotation_identity(
            agent,
            stream_id=stream_id,
            old_session_id="compression-continuation",
            new_session_id="retired-continuation",
        ) is False
        assert agent._live_rotation_parent_session_id == "compression-origin"
        assert config.ACTIVE_RUNS[stream_id]["session_id"] == "compression-continuation"
    finally:
        streaming.STREAMS.pop(stream_id, None)
        streaming.AGENT_INSTANCES.pop(stream_id, None)
        config.ACTIVE_RUNS.pop(stream_id, None)


def test_chain_agent_event_callback_preserves_existing_observer():
    from api.streaming import _chain_agent_event_callback

    calls = []
    agent = SimpleNamespace(
        event_callback=lambda event_name, payload: calls.append(
            ("existing", event_name, payload)
        )
    )
    _chain_agent_event_callback(
        agent,
        lambda event_name, payload: calls.append(("webui", event_name, payload)),
    )

    payload = {"session_id": "continuation", "old_session_id": "origin"}
    agent.event_callback("session:compress", payload)

    assert calls == [
        ("existing", "session:compress", payload),
        ("webui", "session:compress", payload),
    ]


def test_handle_chat_steer_evicts_mismatched_cached_agent(monkeypatch):
    import api.streaming as streaming
    from api.streaming import _handle_chat_steer

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
    handler = Handler()

    _handle_chat_steer(handler, {"session_id": "requested", "text": "please steer"})

    payload = json.loads(handler.wfile.getvalue().decode("utf-8"))
    assert handler.status == 200
    assert payload == {"accepted": False, "fallback": "no_cached_agent", "stream_id": None}
    assert "requested" not in config.SESSION_AGENT_CACHE
    assert closed_entries == [("requested", (wrong_agent, "sig"))]

    config.SESSION_AGENT_CACHE.clear()
