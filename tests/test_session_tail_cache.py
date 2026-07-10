import json
import os
import stat
import time
from pathlib import Path

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


def test_save_writes_atomic_v1_raw_tail_schema_and_permissions(isolated_session_store):
    messages = _messages(305)
    messages[1] = {
        "role": "tool",
        "content": json.dumps({"todos": [{"content": "old but current", "status": "pending"}]}),
        "timestamp": 2.0,
        "custom": "todo-raw-shape",
    }
    tool_calls = [
        {"name": "before-tail", "assistant_msg_idx": 3, "custom": {"raw": True}},
        {"name": "inside-tail", "assistant_msg_idx": 303, "custom": [1, 2]},
    ]
    session = Session(
        session_id="tail_schema",
        profile="default",
        messages=messages,
        tool_calls=tool_calls,
    )

    session.save(skip_index=True)

    cache_path = models.session_tail_cache_path(session.session_id)
    assert cache_path == isolated_session_store / "_tail_cache" / "v1" / "tail_schema.json"
    payload = json.loads(cache_path.read_bytes())
    assert payload["format"] == "hermes.session-tail-cache"
    assert payload["version"] == 1
    assert payload["session_id"] == session.session_id
    assert payload["tail_limit"] == 300
    assert payload["source_message_count"] == 305
    assert payload["source_user_message_count"] == sum(m.get("role") == "user" for m in messages)
    assert payload["source_last_message_at"] == 305.0
    assert payload["message_offset"] == 5
    assert payload["messages"] == messages[-300:]
    assert payload["tool_calls"] == [tool_calls[1]]
    assert payload["all_tool_calls_positionable"] is True
    assert payload["todo_state"]["todos"] == [
        {"content": "old but current", "status": "pending"}
    ]
    assert payload["anchor_scene_index"] == {}
    assert isinstance(payload["created_at"], float)
    signature = payload["source_signature"]
    assert Path(signature["path"]).is_absolute()
    assert signature == models._tail_cache_signature_dict(
        models._sidecar_stat_signature(session.path)
    )
    if os.name != "nt":
        assert stat.S_IMODE(cache_path.stat().st_mode) == 0o600


def test_reader_returns_validated_snapshot_without_mutating_sidecar(isolated_session_store):
    session = Session(
        session_id="tail_read",
        profile="default",
        messages=_messages(4),
    )
    session.save(skip_index=True)
    sidecar_before = session.path.read_bytes()

    snapshot = models.read_session_tail_cache(session.session_id)

    assert snapshot is not None
    assert snapshot["messages"] == session.messages
    assert snapshot["message_offset"] == 0
    assert session.path.read_bytes() == sidecar_before


def test_reader_accepts_finite_zero_timestamp(isolated_session_store):
    session = Session(
        session_id="tail_zero_timestamp",
        messages=[{"role": "user", "content": "epoch", "timestamp": 0.0}],
    )
    session.save(skip_index=True)

    snapshot = models.read_session_tail_cache(session.session_id)

    assert snapshot is not None
    assert snapshot["messages"][0]["timestamp"] == 0.0


def test_save_is_fail_open_when_tail_cache_write_fails(isolated_session_store, monkeypatch):
    session = Session(session_id="tail_fail_open", messages=_messages(2))

    def fail_cache(*args, **kwargs):
        raise OSError("cache disk unavailable")

    monkeypatch.setattr(models, "_write_session_tail_cache", fail_cache)

    session.save(skip_index=True)

    assert json.loads(session.path.read_bytes())["messages"] == session.messages


def test_stable_full_load_opportunistically_builds_but_metadata_only_never_builds(
    isolated_session_store,
):
    session = Session(
        session_id="tail_load_build",
        profile="default",
        messages=_messages(3),
    )
    session.save(skip_index=True)
    cache_path = models.session_tail_cache_path(session.session_id)
    cache_path.unlink()

    metadata = Session.load_metadata_only(session.session_id)
    assert metadata is not None
    assert getattr(metadata, "_loaded_metadata_only", False) is True
    assert not cache_path.exists()

    loaded = Session.load(session.session_id)
    assert loaded is not None
    assert loaded.messages == session.messages
    assert cache_path.exists()


@pytest.mark.parametrize(
    "mutate",
    [
        lambda payload: payload.update(version=999),
        lambda payload: payload.update(message_offset=999),
        lambda payload: payload.update(all_tool_calls_positionable=False),
        lambda payload: payload.update(anchor_scene_index={"scene": 1.0}),
        lambda payload: payload["messages"][-1].pop("timestamp"),
    ],
)
def test_reader_rejects_incompatible_or_incomplete_payloads(
    isolated_session_store,
    mutate,
):
    session = Session(session_id="tail_invalid", messages=_messages(4))
    session.save(skip_index=True)
    cache_path = models.session_tail_cache_path(session.session_id)
    payload = json.loads(cache_path.read_bytes())
    mutate(payload)
    cache_path.write_text(json.dumps(payload), encoding="utf-8")

    assert models.read_session_tail_cache(session.session_id) is None


def test_reader_rejects_stale_and_oversize_cache(isolated_session_store):
    session = Session(session_id="tail_stale", messages=_messages(4))
    session.save(skip_index=True)
    cache_path = models.session_tail_cache_path(session.session_id)

    session.path.write_text(session.path.read_text(encoding="utf-8") + "\n", encoding="utf-8")
    assert models.read_session_tail_cache(session.session_id) is None

    cache_path.write_bytes(b"x" * (models._SESSION_TAIL_CACHE_MAX_BYTES + 1))
    assert models.read_session_tail_cache(session.session_id) is None


def test_reader_restats_sidecar_and_rejects_toctou(isolated_session_store, monkeypatch):
    session = Session(session_id="tail_toctou_read", messages=_messages(4))
    session.save(skip_index=True)
    original = models._sidecar_stat_signature(session.path)
    assert original is not None
    changed = (original[0], original[1] + 1, original[2], original[3] + 1)
    calls = 0

    def changing_signature(_path):
        nonlocal calls
        calls += 1
        return original if calls == 1 else changed

    monkeypatch.setattr(models, "_sidecar_stat_signature", changing_signature)

    assert models.read_session_tail_cache(session.session_id) is None


def test_writer_does_not_publish_when_sidecar_changes_before_replace(
    isolated_session_store,
    monkeypatch,
):
    session = Session(session_id="tail_toctou_write", messages=_messages(4))
    session.save(skip_index=True)
    cache_path = models.session_tail_cache_path(session.session_id)
    cache_path.unlink()
    expected = models._sidecar_stat_signature(session.path)
    assert expected is not None
    changed = (expected[0], expected[1] + 1, expected[2], expected[3] + 1)
    monkeypatch.setattr(models, "_sidecar_stat_signature", lambda _path: changed)

    assert models._write_session_tail_cache(session, expected_signature=expected) is False
    assert not cache_path.exists()


def test_reader_never_supersedes_full_in_memory_session(isolated_session_store):
    session = Session(session_id="tail_memory_ahead", messages=_messages(4))
    session.save(skip_index=True)
    session.messages.append(
        {"role": "assistant", "content": "unsaved", "timestamp": 99.0}
    )
    models.SESSIONS[session.session_id] = session

    assert models.read_session_tail_cache(session.session_id) is None


def test_unpositionable_tool_calls_and_scenes_make_cache_ineligible(isolated_session_store):
    session = Session(
        session_id="tail_ineligible",
        messages=_messages(4),
        tool_calls=[{"name": "missing-index"}],
        anchor_activity_scenes={"scene": {"updated_at": 1.0}},
    )
    session.save(skip_index=True)

    assert models.read_session_tail_cache(session.session_id) is None


def test_opportunistic_full_load_does_not_write_foreign_profile_cache(
    isolated_session_store,
    monkeypatch,
):
    session = Session(session_id="tail_foreign", profile="foreign", messages=_messages(4))
    session.save(skip_index=True)
    cache_path = models.session_tail_cache_path(session.session_id)
    cache_path.unlink()
    monkeypatch.setattr(models, "_session_tail_cache_profile_is_active", lambda _session: False)

    loaded = Session.load(session.session_id)

    assert loaded is not None
    assert not cache_path.exists()


def test_delete_tail_cache_is_best_effort_and_path_safe(isolated_session_store):
    session = Session(session_id="tail_delete", messages=_messages(4))
    session.save(skip_index=True)
    cache_path = models.session_tail_cache_path(session.session_id)
    assert cache_path.exists()

    assert models.delete_session_tail_cache(session.session_id) is True
    assert not cache_path.exists()
    assert models.delete_session_tail_cache(session.session_id) is True
    assert models.delete_session_tail_cache("../unsafe") is False


def test_stale_cache_tmp_cleanup_and_nested_cache_discovery_isolation(isolated_session_store):
    session = Session(session_id="tail_discovery", messages=_messages(2))
    session.save(skip_index=True)
    cache_path = models.session_tail_cache_path(session.session_id)
    stale_tmp = cache_path.with_name(f"{cache_path.name}.tmp.1.2")
    stale_tmp.write_text("stale", encoding="utf-8")
    old = time.time() - models._STALE_TMP_AGE_SECONDS - 10
    os.utime(stale_tmp, (old, old))

    models._cleanup_stale_tmp_files()

    assert not stale_tmp.exists()
    assert [path.name for path in isolated_session_store.glob("*.json")] == [
        "tail_discovery.json"
    ]
