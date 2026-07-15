import json
import os
import stat
import threading
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
    # Most storage tests exercise the cache format with compact fixtures. Source
    # threshold behavior is covered explicitly below with the production limit.
    monkeypatch.setattr(models, "_SESSION_TAIL_CACHE_MIN_SOURCE_BYTES", 0)
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


def _writer_messages(label, count):
    messages = _messages(count)
    messages[-2] = {
        "role": "tool",
        "content": json.dumps(
            {
                "todos": [
                    {
                        "id": f"todo-{label}",
                        "content": f"todo from {label}",
                        "status": "pending",
                    }
                ]
            }
        ),
        "timestamp": float(count - 1),
    }
    messages[-1] = {
        "role": "assistant",
        "content": f"FINAL_FROM_{label}",
        "timestamp": float(count),
    }
    return messages


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


def test_save_publishes_cache_only_after_source_reaches_threshold(
    isolated_session_store,
    monkeypatch,
):
    monkeypatch.setattr(
        models,
        "_SESSION_TAIL_CACHE_MIN_SOURCE_BYTES",
        1 * 1024 * 1024,
    )
    session = Session(
        session_id="tail_source_threshold",
        messages=_messages(4),
    )
    cache_path = models.session_tail_cache_path(session.session_id)

    session.save(skip_index=True)

    assert session.path.exists()
    assert session.path.stat().st_size < models._SESSION_TAIL_CACHE_MIN_SOURCE_BYTES
    assert not cache_path.exists()

    session.messages.append(
        {
            "role": "assistant",
            "content": "x" * (models._SESSION_TAIL_CACHE_MIN_SOURCE_BYTES + 1024),
            "timestamp": 99.0,
        }
    )
    session.save(skip_index=True)

    assert session.path.stat().st_size >= models._SESSION_TAIL_CACHE_MIN_SOURCE_BYTES
    assert cache_path.exists()
    assert models.read_session_tail_cache(session.session_id) is not None


def test_same_id_concurrent_saves_bind_cache_to_authoritative_writer(
    isolated_session_store,
    monkeypatch,
):
    session_id = "tail_same_id_race"
    messages_a = _writer_messages("A", 306)
    messages_b = _writer_messages("B", 308)
    writer_a = Session(
        session_id=session_id,
        messages=messages_a,
        tool_calls=[{"name": "tool-A", "assistant_msg_idx": len(messages_a) - 1}],
    )
    writer_b = Session(
        session_id=session_id,
        messages=messages_b,
        tool_calls=[{"name": "tool-B", "assistant_msg_idx": len(messages_b) - 1}],
    )
    cache_path = models.session_tail_cache_path(session_id)
    a_replaced_source = threading.Event()
    b_reached_source_replace = threading.Event()
    b_published_cache = threading.Event()
    release_a = threading.Event()
    original_replace = models._safe_replace

    def observed_replace(src, dst):
        current_name = threading.current_thread().name
        if current_name == "writer-b" and dst == writer_b.path:
            b_reached_source_replace.set()
        original_replace(src, dst)
        if current_name == "writer-a" and dst == writer_a.path:
            a_replaced_source.set()
            assert release_a.wait(timeout=5), "writer A was not released"
        if current_name == "writer-b" and dst == cache_path:
            b_published_cache.set()

    monkeypatch.setattr(models, "_safe_replace", observed_replace)
    errors = []

    def save(session):
        try:
            session.save(touch_updated_at=False, skip_index=True)
        except BaseException as exc:  # pragma: no cover - asserted below
            errors.append(exc)

    thread_a = threading.Thread(target=save, args=(writer_a,), name="writer-a")
    thread_b = threading.Thread(target=save, args=(writer_b,), name="writer-b")
    thread_a.start()
    assert a_replaced_source.wait(timeout=5), "writer A did not publish its source"
    thread_b.start()
    try:
        if b_reached_source_replace.wait(timeout=0.5):
            assert b_published_cache.wait(timeout=5), "writer B did not publish its cache"
    finally:
        release_a.set()
    thread_a.join(timeout=5)
    thread_b.join(timeout=5)

    assert not thread_a.is_alive()
    assert not thread_b.is_alive()
    assert errors == []
    authoritative = json.loads(writer_a.path.read_bytes())
    assert authoritative["messages"] == messages_b
    snapshot = models.read_session_tail_cache(session_id)
    assert snapshot is not None
    assert snapshot["messages"] == messages_b[-models._SESSION_TAIL_CACHE_LIMIT :]
    assert snapshot["source_message_count"] == len(messages_b)
    assert snapshot["source_user_message_count"] == sum(
        message.get("role") == "user" for message in messages_b
    )
    assert snapshot["tool_calls"] == writer_b.tool_calls
    assert snapshot["todo_state"]["todos"][0]["id"] == "todo-B"
    assert snapshot["source_signature"] == models._tail_cache_signature_dict(
        models._sidecar_stat_signature(writer_b.path)
    )


def test_session_save_lock_is_per_id_not_global(isolated_session_store, monkeypatch):
    session_a = Session(session_id="tail_lock_a", messages=_messages(4))
    session_b = Session(session_id="tail_lock_b", messages=_messages(4))
    a_entered_cache_publication = threading.Event()
    b_entered_cache_publication = threading.Event()
    release_a = threading.Event()
    original_writer = models._write_session_tail_cache_payload

    def observed_writer(payload, *, expected_signature):
        if payload["session_id"] == session_a.session_id:
            a_entered_cache_publication.set()
            assert release_a.wait(timeout=5), "session A was not released"
        elif payload["session_id"] == session_b.session_id:
            b_entered_cache_publication.set()
        return original_writer(payload, expected_signature=expected_signature)

    monkeypatch.setattr(models, "_write_session_tail_cache_payload", observed_writer)
    errors = []

    def save(session):
        try:
            session.save(touch_updated_at=False, skip_index=True)
        except BaseException as exc:  # pragma: no cover - asserted below
            errors.append(exc)

    thread_a = threading.Thread(target=save, args=(session_a,))
    thread_b = threading.Thread(target=save, args=(session_b,))
    thread_a.start()
    assert a_entered_cache_publication.wait(timeout=5)
    thread_b.start()
    try:
        assert b_entered_cache_publication.wait(timeout=2), (
            "different session IDs were serialized by a global save lock"
        )
    finally:
        release_a.set()
    thread_a.join(timeout=5)
    thread_b.join(timeout=5)

    assert not thread_a.is_alive()
    assert not thread_b.is_alive()
    assert errors == []


def _large_messages_with_marker(marker):
    return [
        {
            "role": "user",
            "content": marker + ("x" * (models._SESSION_TAIL_CACHE_MIN_SOURCE_BYTES + 1024)),
            "timestamp": 1.0,
        },
        {
            "role": "assistant",
            "content": "large response",
            "timestamp": 2.0,
        },
    ]


def test_large_to_small_save_removes_tail_cache_bytes(isolated_session_store, monkeypatch):
    monkeypatch.setattr(models, "_SESSION_TAIL_CACHE_MIN_SOURCE_BYTES", 1 * 1024 * 1024)
    marker = "STALE_LARGE_TO_SMALL_SECRET"
    session = Session(
        session_id="tail_large_to_small",
        messages=_large_messages_with_marker(marker),
    )
    cache_path = models.session_tail_cache_path(session.session_id)
    session.save(touch_updated_at=False, skip_index=True)
    assert cache_path.exists()
    assert marker.encode() in cache_path.read_bytes()

    session.messages = _messages(2)
    session.save(touch_updated_at=False, skip_index=True)

    assert session.path.stat().st_size < models._SESSION_TAIL_CACHE_MIN_SOURCE_BYTES
    assert not cache_path.exists()
    assert models.read_session_tail_cache(session.session_id) is None


def test_large_to_empty_save_removes_tail_cache_bytes(isolated_session_store, monkeypatch):
    monkeypatch.setattr(models, "_SESSION_TAIL_CACHE_MIN_SOURCE_BYTES", 1 * 1024 * 1024)
    marker = "STALE_LARGE_TO_EMPTY_SECRET"
    session = Session(
        session_id="tail_large_to_empty",
        messages=_large_messages_with_marker(marker),
    )
    cache_path = models.session_tail_cache_path(session.session_id)
    session.save(touch_updated_at=False, skip_index=True)
    assert cache_path.exists()
    assert marker.encode() in cache_path.read_bytes()

    session.messages = []
    session.save(touch_updated_at=False, skip_index=True)

    assert json.loads(session.path.read_bytes())["messages"] == []
    assert not cache_path.exists()
    assert models.read_session_tail_cache(session.session_id) is None


def test_cache_cleanup_failure_is_nonfatal_after_authoritative_save(
    isolated_session_store,
    monkeypatch,
    caplog,
):
    monkeypatch.setattr(models, "_SESSION_TAIL_CACHE_MIN_SOURCE_BYTES", 1 * 1024 * 1024)
    session = Session(
        session_id="tail_cleanup_failure",
        messages=_large_messages_with_marker("STALE_CLEANUP_FAILURE_SECRET"),
    )
    cache_path = models.session_tail_cache_path(session.session_id)
    session.save(touch_updated_at=False, skip_index=True)
    assert cache_path.exists()
    attempted = []
    original_unlink = Path.unlink

    def fail_cache_unlink(path, *args, **kwargs):
        if path == cache_path:
            attempted.append(path)
            raise OSError("simulated tail-cache unlink failure")
        return original_unlink(path, *args, **kwargs)

    monkeypatch.setattr(Path, "unlink", fail_cache_unlink)
    new_messages = _messages(2)
    session.messages = new_messages
    caplog.set_level("DEBUG", logger="api.models")

    session.save(touch_updated_at=False, skip_index=True)

    assert attempted == [cache_path]
    assert json.loads(session.path.read_bytes())["messages"] == new_messages
    assert cache_path.exists()
    assert models.read_session_tail_cache(session.session_id) is None
    assert "Failed to remove tail cache" in caplog.text


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


def test_source_size_gate_skips_stat_when_full_session_is_cached(
    isolated_session_store,
    monkeypatch,
):
    session = Session(session_id="tail_cached_full", messages=_messages(4))
    models.SESSIONS[session.session_id] = session

    def unexpected_stat(_path):
        raise AssertionError("warm full-session path must not stat the sidecar")

    monkeypatch.setattr(models, "_sidecar_stat_signature", unexpected_stat)

    assert models.session_tail_cache_source_is_large_enough(session.session_id) is False


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
