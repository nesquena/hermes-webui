"""Regression coverage for #5532 clear-route state.db replay suppression."""

from __future__ import annotations

import json
from collections import OrderedDict
from io import BytesIO
from types import SimpleNamespace

import pytest

pytestmark = pytest.mark.requires_agent_modules


def _msg(role: str, content: str, ts: float, mid: str) -> dict:
    return {"id": mid, "role": role, "content": content, "timestamp": ts}


def _install_isolated_session_env(monkeypatch, tmp_path):
    import api.config as config
    import api.models as models
    import api.profiles as profiles
    import api.routes as routes

    monkeypatch.setattr(config, "STATE_DIR", tmp_path, raising=False)
    session_dir = tmp_path / "sessions"
    monkeypatch.setattr(config, "SESSION_DIR", session_dir, raising=False)
    monkeypatch.setattr(config, "SESSION_INDEX_FILE", session_dir / "_index.json", raising=False)
    monkeypatch.setattr(models, "SESSION_DIR", session_dir, raising=False)
    monkeypatch.setattr(models, "SESSION_INDEX_FILE", session_dir / "_index.json", raising=False)
    monkeypatch.setattr(models, "SESSIONS", OrderedDict(), raising=False)
    monkeypatch.setattr(profiles, "get_active_hermes_home", lambda: tmp_path, raising=False)
    monkeypatch.setattr(models, "_active_state_db_path", lambda: tmp_path / "state.db", raising=False)
    monkeypatch.setattr(routes, "_active_state_db_path", lambda: tmp_path / "state.db", raising=False)
    monkeypatch.setattr(config, "_evict_session_agent", lambda _sid: None, raising=False)
    session_dir.mkdir(parents=True, exist_ok=True)
    return session_dir


def _post_clear(monkeypatch, sid: str):
    import api.routes as routes

    body = b'{"session_id":"%s"}' % sid.encode("utf-8")
    monkeypatch.setattr(routes, "_check_csrf", lambda handler: True)

    captured = {}

    def fake_j(_handler, payload, status=200, extra_headers=None):
        captured["payload"] = payload
        captured["status"] = status
        captured["extra_headers"] = extra_headers

    monkeypatch.setattr(routes, "j", fake_j)

    handler = SimpleNamespace(
        headers={"Content-Length": str(len(body))},
        rfile=BytesIO(body),
    )
    routes.handle_post(handler, SimpleNamespace(path="/api/session/clear"))
    return captured


def test_session_clear_persists_empty_context_and_blocks_state_db_replay(monkeypatch, tmp_path):
    import api.models as models
    from api.models import Session, merge_session_messages_append_only
    from api.session_recovery import inspect_session_recovery_status, recover_session

    _install_isolated_session_env(monkeypatch, tmp_path)

    sid = "issue5532_clear_replay"
    session = Session(
        session_id=sid,
        title="Reconcile",
        workspace=str(tmp_path),
        model="test-model",
        messages=[
            _msg("user", "live prompt", 1.0, "u1"),
            _msg("assistant", "live reply", 2.0, "a1"),
        ],
        context_messages=[
            _msg("user", "live prompt", 1.0, "cu1"),
            _msg("assistant", "live reply", 2.0, "ca1"),
        ],
        created_at=1000.0,
        updated_at=1001.0,
    )
    session.tool_calls = [{"id": "call-1", "function": {"name": "terminal"}}]
    session.truncation_watermark = 9.0
    session.truncation_boundary = 8.0
    session.active_stream_id = "stale-stream"
    session.pending_user_message = "pending prompt"
    session.pending_attachments = [{"name": "pending.txt"}]
    session.pending_started_at = 1002.0
    session.pending_user_source = "webui"
    session.save(touch_updated_at=False)
    session.path.with_suffix(".json.bak").write_text(
        json.dumps(
            {
                "session_id": sid,
                "messages": [_msg("user", "older backup prompt", 0.5, "bu1")],
                "context_messages": [_msg("user", "older backup prompt", 0.5, "bcu1")],
            }
        ),
        encoding="utf-8",
    )

    captured = _post_clear(monkeypatch, sid)

    assert captured["status"] == 200
    assert captured["payload"]["ok"] is True
    assert captured["payload"]["session"]["active_stream_id"] is None
    assert captured["payload"]["session"]["pending_user_message"] is None

    loaded = Session.load(sid)
    assert loaded is not None
    assert loaded.messages == []
    assert loaded.context_messages == []
    assert loaded.tool_calls == []
    assert loaded.truncation_watermark == 0.0
    assert loaded.truncation_boundary == 0.0
    assert loaded.active_stream_id is None
    assert loaded.pending_user_message is None
    assert loaded.pending_attachments == []
    assert loaded.pending_started_at is None
    assert loaded.pending_user_source is None
    assert loaded.clear_generation
    assert loaded.title == "Untitled"

    persisted = json.loads(loaded.path.read_text(encoding="utf-8"))
    assert persisted["messages"] == []
    assert persisted["context_messages"] == []
    assert persisted["truncation_watermark"] == 0.0
    assert persisted["truncation_boundary"] == 0.0
    assert persisted["active_stream_id"] is None
    assert persisted["pending_user_message"] is None
    assert persisted["pending_attachments"] == []
    assert persisted["pending_started_at"] is None
    assert persisted["pending_user_source"] is None
    assert persisted["clear_generation"] == loaded.clear_generation

    state_db_messages = [
        _msg("user", "state prompt", 100.0, "s-u1"),
        _msg("assistant", "state reply", 101.0, "s-a1"),
    ]
    merged = merge_session_messages_append_only(
        loaded.messages,
        state_db_messages,
        truncation_watermark=loaded.truncation_watermark,
        truncation_boundary=loaded.truncation_boundary,
    )
    assert merged == []
    assert models.Session.load(sid).context_messages == []

    assert not loaded.path.with_suffix(".json.bak").exists()
    status = inspect_session_recovery_status(loaded.path)
    assert status["recommend"] == "no_backup"
    recovered = recover_session(loaded.path)
    assert recovered["restored"] is False
    assert Session.load(sid).messages == []


def test_empty_sidecar_without_watermark_still_recovers_state_db_rows():
    from api.models import Session, merge_session_messages_append_only

    session = Session(
        session_id="issue5532_negative_space",
        messages=[],
        context_messages=[],
        truncation_watermark=None,
        truncation_boundary=None,
    )
    state_db_messages = [
        _msg("user", "state prompt", 100.0, "s-u1"),
        _msg("assistant", "state reply", 101.0, "s-a1"),
    ]

    merged = merge_session_messages_append_only(
        session.messages,
        state_db_messages,
        truncation_watermark=session.truncation_watermark,
        truncation_boundary=session.truncation_boundary,
    )

    assert [m["content"] for m in merged] == ["state prompt", "state reply"]


def test_clear_sentinel_does_not_suppress_later_backup_recovery(tmp_path):
    from api.session_recovery import inspect_session_recovery_status, recover_session

    live_path = tmp_path / "post_clear_loss.json"
    live = {
        "session_id": "post_clear_loss",
        "messages": [],
        "context_messages": [],
        "truncation_watermark": 0.0,
        "truncation_boundary": 0.0,
        "active_stream_id": None,
        "pending_user_message": None,
        "pending_attachments": [],
        "pending_started_at": None,
        "pending_user_source": None,
        "clear_generation": "clear-post",
    }
    backup = {
        **live,
        "messages": [_msg("user", "post-clear prompt", 10.0, "u10")],
        "context_messages": [_msg("user", "post-clear prompt", 10.0, "cu10")],
    }
    live_path.write_text(json.dumps(live), encoding="utf-8")
    live_path.with_suffix(".json.bak").write_text(json.dumps(backup), encoding="utf-8")

    status = inspect_session_recovery_status(live_path)
    assert status["recommend"] == "restore"
    recovered = recover_session(live_path)
    assert recovered["restored"] is True
    restored = json.loads(live_path.read_text(encoding="utf-8"))
    assert restored["messages"][0]["content"] == "post-clear prompt"


def test_clearing_empty_live_session_preserves_existing_recoverable_backup(monkeypatch, tmp_path):
    from api.models import Session
    from api.session_recovery import inspect_session_recovery_status, recover_session

    _install_isolated_session_env(monkeypatch, tmp_path)
    sid = "issue5532_empty_live_with_backup"
    session = Session(
        session_id=sid,
        title="Already empty",
        workspace=str(tmp_path),
        model="test-model",
        messages=[],
        context_messages=[],
        truncation_watermark=0.0,
        truncation_boundary=0.0,
        created_at=1000.0,
        updated_at=1001.0,
    )
    session.save(touch_updated_at=False)
    backup = {
        "session_id": sid,
        "messages": [_msg("user", "recoverable prompt", 10.0, "u10")],
        "context_messages": [_msg("user", "recoverable prompt", 10.0, "cu10")],
        "truncation_watermark": 0.0,
        "truncation_boundary": 0.0,
    }
    session.path.with_suffix(".json.bak").write_text(json.dumps(backup), encoding="utf-8")

    captured = _post_clear(monkeypatch, sid)

    assert captured["status"] == 200
    assert session.path.with_suffix(".json.bak").exists()
    assert Session.load(sid).clear_generation is None
    assert inspect_session_recovery_status(session.path)["recommend"] == "restore"
    recovered = recover_session(session.path)
    assert recovered["restored"] is True
    assert Session.load(sid).messages[0]["content"] == "recoverable prompt"
