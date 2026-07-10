import json
from types import SimpleNamespace
from unittest.mock import patch
from urllib.parse import urlparse

import pytest

import api.models as models
import api.routes as routes
from api.models import Session


@pytest.fixture
def isolated_session_store(tmp_path, monkeypatch):
    session_dir = tmp_path / "sessions"
    session_dir.mkdir()
    monkeypatch.setattr(models, "SESSION_DIR", session_dir)
    monkeypatch.setattr(models, "SESSION_INDEX_FILE", session_dir / "_index.json")
    monkeypatch.setattr(routes, "SESSION_DIR", session_dir)
    monkeypatch.setattr(models, "_active_state_db_path", lambda: tmp_path / "missing-state.db")
    monkeypatch.setattr(models, "_get_profile_home", lambda _profile: tmp_path / "missing-profile")
    models.SESSIONS.clear()
    yield session_dir
    models.SESSIONS.clear()


def _messages(count):
    return [
        {
            "role": "user" if idx % 2 == 0 else "assistant",
            "content": f"message-{idx}",
            "timestamp": float(idx + 1),
        }
        for idx in range(count)
    ]


def _invoke(
    sid,
    *,
    query_suffix="&messages=1&resolve_model=0&msg_limit=30",
    prefix_summary=None,
    state_db_messages=None,
):
    captured = {}

    def fake_j(_handler, data, status=200, extra_headers=None):
        captured["data"] = data
        captured["status"] = status
        return data

    handler = SimpleNamespace(_safe_webui_print=lambda _text: None)
    parsed = urlparse(f"/api/session?session_id={sid}{query_suffix}")
    if prefix_summary is None:
        prefix_summary = {"count": 0, "null_timestamp_count": 0}
    if state_db_messages is None:
        state_db_messages = []
    with patch("api.routes._clear_stale_stream_state", return_value=False), patch(
        "api.routes._lookup_cli_session_metadata", return_value={}
    ), patch(
        "api.routes.get_state_db_session_messages", return_value=state_db_messages
    ), patch(
        "api.routes.get_state_db_session_message_prefix_summary",
        return_value=prefix_summary,
    ), patch(
        "api.routes.redact_session_data", side_effect=lambda raw: raw
    ), patch("api.routes.j", side_effect=fake_j):
        routes.handle_get(handler, parsed)
    return captured


def _saved_large_session(sid="tail_route"):
    messages = _messages(400)
    session = Session(
        session_id=sid,
        title="Tail route",
        model="gpt-test",
        context_length=1024,
        messages=messages,
        tool_calls=[
            {"name": "old", "assistant_msg_idx": 1},
            {"name": "tail", "assistant_msg_idx": 399},
        ],
    )
    session.save()
    return session


def test_process_cold_cache_hit_skips_full_session_load(isolated_session_store):
    session = _saved_large_session()
    models.SESSIONS.clear()

    with patch.object(
        Session,
        "load",
        side_effect=AssertionError("tail-cache hit must not full-load the sidecar"),
    ):
        captured = _invoke(session.session_id)

    assert captured["status"] == 200
    payload = captured["data"]["session"]
    assert payload["message_count"] == 400
    assert payload["user_message_count"] == 200
    assert payload["_messages_offset"] == 370
    assert payload["messages"] == session.messages[-30:]
    assert payload["tool_calls"] == [{"name": "tail", "assistant_msg_idx": 29}]


def test_cache_payload_is_equivalent_to_full_fallback(isolated_session_store):
    session = _saved_large_session("tail_route_parity")
    session.messages[1] = {
        "role": "tool",
        "content": json.dumps({"todos": [{"content": "persist", "status": "pending"}]}),
        "timestamp": 2.0,
    }
    session.save()
    models.SESSIONS.clear()
    with patch("api.routes.read_session_tail_cache", return_value=None), patch(
        "api.routes.build_session_tail_cache_from_legacy_sidecar", return_value=None
    ):
        full = _invoke(session.session_id)["data"]["session"]

    models.SESSIONS.clear()
    cached = _invoke(session.session_id)["data"]["session"]

    assert cached == full


def test_cache_merges_recent_state_db_delta_with_absolute_counts(isolated_session_store):
    session = _saved_large_session("tail_route_delta")
    delta = [{"role": "assistant", "content": "delta", "timestamp": 401.0}]
    models.SESSIONS.clear()

    cached = _invoke(
        session.session_id,
        state_db_messages=delta,
    )["data"]["session"]

    models.SESSIONS.clear()
    with patch("api.routes.read_session_tail_cache", return_value=None), patch(
        "api.routes.build_session_tail_cache_from_legacy_sidecar", return_value=None
    ):
        full = _invoke(
            session.session_id,
            state_db_messages=delta,
        )["data"]["session"]

    assert cached == full
    assert cached["message_count"] == 401
    assert cached["user_message_count"] == 200
    assert cached["_messages_offset"] == 371


def test_cache_falls_back_when_state_db_has_older_rows(isolated_session_store):
    session = _saved_large_session("tail_route_state_prefix")
    models.SESSIONS.clear()
    original_load = Session.load
    calls = []

    def tracked_load(sid, *args, **kwargs):
        calls.append(sid)
        return original_load(sid, *args, **kwargs)

    with patch.object(Session, "load", side_effect=tracked_load):
        captured = _invoke(
            session.session_id,
            prefix_summary={"count": 1, "null_timestamp_count": 0},
        )

    assert captured["status"] == 200
    assert calls == [session.session_id]
    assert captured["data"]["session"]["message_count"] == 400


def test_msg_before_keeps_full_loader(isolated_session_store):
    session = _saved_large_session("tail_route_msg_before")
    models.SESSIONS.clear()
    original_load = Session.load
    calls = []

    def tracked_load(sid, *args, **kwargs):
        calls.append(sid)
        return original_load(sid, *args, **kwargs)

    with patch.object(Session, "load", side_effect=tracked_load):
        captured = _invoke(
            session.session_id,
            query_suffix="&messages=1&resolve_model=0&msg_limit=30&msg_before=300",
        )

    assert captured["status"] == 200
    assert calls == [session.session_id]
    assert captured["data"]["session"]["_messages_offset"] == 270


def test_scene_session_keeps_full_loader(isolated_session_store):
    session = _saved_large_session("tail_route_scene")
    session.anchor_activity_scenes = {
        "anchor": {"updated_at": 1.0, "payload": {"kind": "activity"}}
    }
    session.save()
    models.SESSIONS.clear()
    original_load = Session.load
    calls = []

    def tracked_load(sid, *args, **kwargs):
        calls.append(sid)
        return original_load(sid, *args, **kwargs)

    with patch.object(Session, "load", side_effect=tracked_load):
        captured = _invoke(session.session_id)

    assert captured["status"] == 200
    assert calls == [session.session_id]


def test_full_in_memory_session_wins_over_disk_tail_cache(isolated_session_store):
    session = _saved_large_session("tail_route_memory_ahead")
    unsaved = {"role": "assistant", "content": "memory-ahead", "timestamp": 401.0}
    session.messages.append(unsaved)
    models.SESSIONS[session.session_id] = session

    with patch.object(
        Session,
        "load",
        side_effect=AssertionError("full in-memory session must not reload disk"),
    ):
        captured = _invoke(session.session_id)

    payload = captured["data"]["session"]
    assert captured["status"] == 200
    assert payload["message_count"] == 401
    assert payload["messages"][-1] == unsaved


def test_profile_mismatch_does_not_build_or_parse_tail(isolated_session_store):
    session = _saved_large_session("tail_route_foreign")
    session.profile = "foreign-profile"
    session.save()
    models.delete_session_tail_cache(session.session_id)
    models.SESSIONS.clear()

    with patch(
        "api.routes.build_session_tail_cache_from_legacy_sidecar",
        side_effect=AssertionError("foreign profile must not warm tail cache"),
    ), patch.object(
        Session,
        "load",
        side_effect=AssertionError("foreign profile must not full-load body"),
    ):
        captured = _invoke(session.session_id)

    assert captured["status"] == 409
    assert captured["data"]["code"] == "session_profile_mismatch"
    assert not models.session_tail_cache_path(session.session_id).exists()
