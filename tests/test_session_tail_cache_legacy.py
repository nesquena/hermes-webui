import json

import pytest

import api.models as models
from api.models import Session


@pytest.fixture
def isolated_session_store(tmp_path, monkeypatch):
    session_dir = tmp_path / "sessions"
    session_dir.mkdir()
    monkeypatch.setattr(models, "SESSION_DIR", session_dir)
    monkeypatch.setattr(models, "SESSION_INDEX_FILE", session_dir / "_index.json")
    models.SESSIONS.clear()
    yield session_dir
    models.SESSIONS.clear()


def _messages(count):
    return [
        {
            "role": "user" if idx % 2 == 0 else "assistant",
            "content": {"raw": idx, "parts": [idx, {"nested": True}]},
            "timestamp": float(idx + 1),
            "custom": ["preserve", idx],
        }
        for idx in range(count)
    ]


def _write_legacy_sidecar(session_dir, sid, messages, *, indent=None, **overrides):
    updated_at = float(messages[-1]["timestamp"] if messages else 1.0)
    payload = {
        "session_id": sid,
        "title": "Legacy tail fixture",
        "workspace": str(session_dir),
        "model": "test-model",
        "created_at": 1.0,
        "updated_at": updated_at,
        "profile": "default",
        "active_stream_id": None,
        "pending_user_message": None,
        "pending_attachments": [],
        "pending_started_at": None,
        "pending_user_source": None,
        "pre_compression_snapshot": False,
        "compression_recovery": {},
        "truncation_watermark": None,
        "truncation_boundary": None,
        "parent_session_id": None,
        "is_cli_session": False,
        "source_tag": "webui",
        "raw_source": None,
        "session_source": "webui",
        "source_label": None,
        "read_only": False,
        "message_count": len(messages),
        "messages": messages,
        "tool_calls": [],
        "anchor_activity_scenes": {},
    }
    payload.update(overrides)
    path = session_dir / f"{sid}.json"
    path.write_text(
        json.dumps(
            payload,
            ensure_ascii=False,
            indent=indent,
            separators=None if indent else (",", ":"),
        ),
        encoding="utf-8",
    )
    row = {
        "session_id": sid,
        "message_count": len(messages),
        "user_message_count": sum(message.get("role") == "user" for message in messages),
        "last_message_at": updated_at,
        "file_size": path.stat().st_size,
        "updated_at": updated_at,
    }
    (session_dir / "_index.json").write_text(json.dumps([row]), encoding="utf-8")
    return path, row


@pytest.mark.parametrize("indent", [None, 2])
def test_legacy_locator_builds_valid_tail_without_full_session_load(
    isolated_session_store,
    monkeypatch,
    indent,
):
    messages = _messages(305)
    sid = f"legacy_locator_{indent or 0}"
    tool_call = {"name": "recent", "assistant_msg_idx": 303}
    _write_legacy_sidecar(
        isolated_session_store,
        sid,
        messages,
        indent=indent,
        tool_calls=[tool_call],
    )
    monkeypatch.setattr(
        Session,
        "load",
        classmethod(lambda cls, _sid, **_kwargs: (_ for _ in ()).throw(AssertionError("full load"))),
    )

    snapshot = models.build_session_tail_cache_from_legacy_sidecar(sid)

    assert snapshot is not None
    assert snapshot["message_offset"] == 5
    assert snapshot["messages"] == messages[-300:]
    assert snapshot["tool_calls"] == [tool_call]
    assert models.read_session_tail_cache(sid) == snapshot


@pytest.mark.parametrize("ambiguity", ["index-count", "todo-before-tail", "scene"])
def test_legacy_locator_falls_back_on_ambiguous_semantics(
    isolated_session_store,
    ambiguity,
):
    messages = _messages(305)
    if ambiguity == "todo-before-tail":
        messages[0]["content"] = json.dumps({"todos": []})
    kwargs = {}
    if ambiguity == "scene":
        kwargs["anchor_activity_scenes"] = {"scene": {"updated_at": 1.0}}
    sid = f"legacy_ambiguous_{ambiguity.replace('-', '_')}"
    _path, row = _write_legacy_sidecar(isolated_session_store, sid, messages, **kwargs)
    if ambiguity == "index-count":
        row["message_count"] -= 1
        (isolated_session_store / "_index.json").write_text(
            json.dumps([row]),
            encoding="utf-8",
        )

    assert models.build_session_tail_cache_from_legacy_sidecar(sid) is None
    assert not models.session_tail_cache_path(sid).exists()
