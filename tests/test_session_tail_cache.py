import gc
import json
import os
import stat
import threading
import time
import weakref
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


def _message(index):
    return {
        "role": "user" if index % 2 == 0 else "assistant",
        "content": f"message-{index}",
        "timestamp": float(index + 1),
    }


def _same_size_replacement_with_extra_message(path: Path) -> bytes:
    source = json.loads(path.read_bytes())
    original_size = len(path.read_bytes())
    original_title = source["title"]
    source["messages"].append(_message(len(source["messages"])))
    expanded = json.dumps(source, ensure_ascii=False, indent=2).encode("utf-8")
    delta = len(expanded) - original_size
    assert delta > 0
    assert len(original_title) > delta
    source["title"] = original_title[:-delta]
    replacement = json.dumps(source, ensure_ascii=False, indent=2).encode("utf-8")
    assert len(replacement) == original_size
    return replacement


def _replace_bytes(path: Path, replacement: bytes, mode: str) -> None:
    if mode == "in_place":
        path.write_bytes(replacement)
        return
    temp = path.with_name(f"{path.name}.external")
    temp.write_bytes(replacement)
    os.replace(temp, path)


def _force_signature_collision(monkeypatch, path: Path, signature) -> None:
    original_signature = models._sidecar_stat_signature

    def colliding_signature(candidate):
        if Path(candidate) == path:
            return signature
        return original_signature(candidate)

    monkeypatch.setattr(models, "_sidecar_stat_signature", colliding_signature)


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


def test_tail_cache_uses_exact_nested_snapshot_serialized_to_authoritative_source(
    isolated_session_store,
    monkeypatch,
):
    session = Session(
        session_id="nested_snapshot_binding",
        messages=[
            {"role": "user", "content": {"value": "SOURCE_BYTES"}, "timestamp": 1.0},
            {"role": "assistant", "content": "answer", "timestamp": 2.0},
        ],
    )
    source_replaced = threading.Event()
    mutation_done = threading.Event()
    original_replace = models._safe_replace

    def observed_replace(src, dst):
        original_replace(src, dst)
        if dst == session.path:
            source_replaced.set()
            assert mutation_done.wait(timeout=5)

    monkeypatch.setattr(models, "_safe_replace", observed_replace)

    def mutate_nested_message_after_source_publish():
        assert source_replaced.wait(timeout=5)
        session.messages[0]["content"]["value"] = "MUTATED_AFTER_SOURCE"
        mutation_done.set()

    mutator = threading.Thread(target=mutate_nested_message_after_source_publish)
    mutator.start()
    session.save(touch_updated_at=False, skip_index=True)
    mutator.join(timeout=5)
    assert not mutator.is_alive()

    authoritative = json.loads(session.path.read_bytes())
    snapshot = models.read_session_tail_cache(session.session_id)
    assert snapshot is not None
    assert snapshot["messages"] == authoritative["messages"], (
        "accepted cache was derived from nested objects mutated after authoritative "
        "serialization instead of the exact frozen source snapshot"
    )


def test_tail_cache_detaches_nested_message_lists_and_tool_calls_before_replace(
    isolated_session_store,
    monkeypatch,
):
    session = Session(
        session_id="nested_tool_snapshot_binding",
        messages=[
            {
                "role": "user",
                "content": {"parts": ["SOURCE", {"nested": "SOURCE"}]},
                "timestamp": 1.0,
            },
            {"role": "assistant", "content": "answer", "timestamp": 2.0},
        ],
        tool_calls=[
            {
                "name": "source-tool",
                "assistant_msg_idx": 1,
                "custom": {"parts": ["SOURCE", {"nested": "SOURCE"}]},
            }
        ],
    )
    original_replace = models._safe_replace

    def mutate_after_serialization_before_replace(src, dst):
        if dst == session.path:
            session.messages[0]["content"]["parts"][1]["nested"] = "MUTATED"
            session.tool_calls[0]["custom"]["parts"][1]["nested"] = "MUTATED"
        original_replace(src, dst)

    monkeypatch.setattr(models, "_safe_replace", mutate_after_serialization_before_replace)

    session.save(touch_updated_at=False, skip_index=True)

    authoritative = json.loads(session.path.read_bytes())
    snapshot = models.read_session_tail_cache(session.session_id)
    assert snapshot is not None
    assert snapshot["messages"] == authoritative["messages"]
    assert snapshot["tool_calls"] == authoritative["tool_calls"]


def test_same_canonical_sidecar_aliases_share_publication_lock(tmp_path, monkeypatch):
    real = tmp_path / "real"
    real.mkdir()
    nested = real / "nested"
    nested.mkdir()
    alias = tmp_path / "alias"
    try:
        alias.symlink_to(real, target_is_directory=True)
    except OSError as exc:  # pragma: no cover - Windows privilege/filesystem dependent
        pytest.skip(f"directory symlinks unavailable: {exc}")

    absolute = real / "same.json"
    aliases = [
        alias / "same.json",
        Path(os.fspath(real) + "/./nested/../same.json"),
    ]
    monkeypatch.chdir(tmp_path)
    aliases.append(Path("real/same.json"))

    assert not absolute.exists()
    expected_lock = models._get_session_save_publication_lock(absolute)
    for spelling in aliases:
        assert spelling.resolve() == absolute.resolve()
        assert models._get_session_save_publication_lock(spelling) is expected_lock
    assert not absolute.exists(), "lock lookup must not create the sidecar"


def test_publication_lock_key_applies_windows_case_normalization(tmp_path, monkeypatch):
    store = tmp_path / "Sessions"
    store.mkdir()
    monkeypatch.setattr(models.os.path, "normcase", lambda value: value.casefold())

    upper = models._get_session_save_publication_lock(store / "Session.JSON")
    lower = models._get_session_save_publication_lock(store / "session.json")

    assert upper is lower


def test_session_dir_rewiring_to_same_store_reuses_publication_lock(tmp_path, monkeypatch):
    real = tmp_path / "profiles" / "default" / "sessions"
    real.mkdir(parents=True)
    alias = tmp_path / "active-profile-sessions"
    try:
        alias.symlink_to(real, target_is_directory=True)
    except OSError as exc:  # pragma: no cover - Windows privilege/filesystem dependent
        pytest.skip(f"directory symlinks unavailable: {exc}")

    monkeypatch.setattr(models, "SESSION_DIR", real)
    first = Session(session_id="profile_rewire", messages=_messages(2))
    first_lock = models._get_session_save_publication_lock(first.path)

    monkeypatch.setattr(models, "SESSION_DIR", alias)
    second = Session(session_id="profile_rewire", messages=_messages(2))
    second_lock = models._get_session_save_publication_lock(second.path)

    assert first.path.resolve() == second.path.resolve()
    assert second_lock is first_lock
    assert not first.path.exists(), "lock lookup must work before the sidecar exists"


def test_publication_lock_does_not_follow_final_symlink_and_is_name_scoped(tmp_path):
    store = tmp_path / "sessions"
    store.mkdir()
    sidecar = store / "same.json"
    target = store / "attacker-target.json"
    target.write_text("target", encoding="utf-8")

    by_name_lock = models._get_session_save_publication_lock(sidecar)
    try:
        sidecar.symlink_to(target)
    except OSError as exc:  # pragma: no cover - Windows privilege/filesystem dependent
        pytest.skip(f"file symlinks unavailable: {exc}")

    assert sidecar.resolve() == target.resolve()
    assert models._get_session_save_publication_lock(sidecar) is by_name_lock
    assert models._get_session_save_publication_lock(target) is not by_name_lock


def test_publication_lock_is_final_name_scoped_for_hardlink_aliases(tmp_path):
    store = tmp_path / "sessions"
    store.mkdir()
    first = store / "first.json"
    second = store / "second.json"
    first.write_text("source", encoding="utf-8")
    try:
        os.link(first, second)
    except OSError as exc:  # pragma: no cover - filesystem dependent
        pytest.skip(f"hard links unavailable: {exc}")

    assert os.path.samefile(first, second)
    assert models._get_session_save_publication_lock(first) is not (
        models._get_session_save_publication_lock(second)
    )


def test_session_retained_publication_lock_releases_after_gc(
    isolated_session_store,
):
    with models._SESSION_SAVE_PUBLICATION_LOCKS_GUARD:
        models._SESSION_SAVE_PUBLICATION_LOCKS.clear()
    session = Session(session_id="weak_lifetime", messages=[])

    session.save(touch_updated_at=False, skip_index=True)

    lock_ref = weakref.ref(session._save_publication_lock)
    assert len(models._SESSION_SAVE_PUBLICATION_LOCKS) == 1
    assert lock_ref() is not None

    del session
    gc.collect()

    assert lock_ref() is None
    assert len(models._SESSION_SAVE_PUBLICATION_LOCKS) == 0


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
    original_unlink = models.os.unlink

    def fail_cache_unlink(path, *args, **kwargs):
        if Path(path) == cache_path:
            attempted.append(Path(path))
            raise OSError("simulated tail-cache unlink failure")
        return original_unlink(path, *args, **kwargs)

    monkeypatch.setattr(models.os, "unlink", fail_cache_unlink)
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


@pytest.mark.parametrize("replacement_mode", ["in_place", "atomic_replace"])
def test_same_stat_tuple_shrink_backs_up_exact_authoritative_bytes(
    isolated_session_store,
    monkeypatch,
    replacement_mode,
):
    session = Session(
        session_id=f"content_proof_shrink_{replacement_mode}",
        title="P" * 5000,
        messages=[_message(0), _message(1), _message(2)],
    )
    session.save(touch_updated_at=False, skip_index=True)
    path = session.path
    signature = models._sidecar_stat_signature(path)
    replacement = _same_size_replacement_with_extra_message(path)
    _replace_bytes(path, replacement, replacement_mode)
    _force_signature_collision(monkeypatch, path, signature)

    session.save(touch_updated_at=False, skip_index=True)

    backup = path.with_suffix(".json.bak")
    assert backup.read_bytes() == replacement
    assert len(json.loads(path.read_bytes())["messages"]) == 3


@pytest.mark.parametrize("replacement_mode", ["in_place", "atomic_replace"])
def test_same_stat_tuple_reader_rejects_same_count_content_replacement(
    isolated_session_store,
    monkeypatch,
    replacement_mode,
):
    session = Session(
        session_id=f"content_proof_reader_{replacement_mode}",
        messages=[_message(0), _message(1)],
    )
    session.save(touch_updated_at=False, skip_index=True)
    path = session.path
    signature = models._sidecar_stat_signature(path)
    source = json.loads(path.read_bytes())
    source["messages"][0]["content"] = "changed-0"
    replacement = json.dumps(source, ensure_ascii=False, indent=2).encode("utf-8")
    assert len(replacement) == path.stat().st_size
    _replace_bytes(path, replacement, replacement_mode)
    _force_signature_collision(monkeypatch, path, signature)

    assert models.read_session_tail_cache(session.session_id) is None


def test_process_local_source_proof_is_invalidated_by_second_session_writer(
    isolated_session_store,
    monkeypatch,
):
    original = Session(
        session_id="content_proof_second_writer",
        title="P" * 5000,
        messages=[_message(0), _message(1), _message(2)],
    )
    original.save(touch_updated_at=False, skip_index=True)
    original_signature = models._sidecar_stat_signature(original.path)
    replacement = _same_size_replacement_with_extra_message(original.path)
    second = Session(**json.loads(replacement))

    second.save(touch_updated_at=False, skip_index=True)
    second_writer_bytes = original.path.read_bytes()
    assert len(second_writer_bytes) == len(replacement)
    assert len(json.loads(second_writer_bytes)["messages"]) == 4
    _force_signature_collision(monkeypatch, original.path, original_signature)

    original.save(touch_updated_at=False, skip_index=True)

    assert original.path.with_suffix(".json.bak").read_bytes() == second_writer_bytes


def test_parent_symlink_rewire_after_binding_fails_closed(tmp_path, monkeypatch):
    real_a = tmp_path / "real-a"
    real_b = tmp_path / "real-b"
    real_a.mkdir()
    real_b.mkdir()
    alias = tmp_path / "active-sessions"
    try:
        alias.symlink_to(real_a, target_is_directory=True)
    except OSError as exc:  # pragma: no cover - platform/filesystem dependent
        pytest.skip(f"directory symlinks unavailable: {exc}")
    monkeypatch.setattr(models, "SESSION_DIR", alias)
    monkeypatch.setattr(models, "SESSION_INDEX_FILE", alias / "_index.json")
    original_get = models._get_session_save_publication_lock
    swapped = False

    def get_lock_then_rewire(path):
        nonlocal swapped
        lock = original_get(path)
        if not swapped:
            swapped = True
            alias.unlink()
            alias.symlink_to(real_b, target_is_directory=True)
        return lock

    monkeypatch.setattr(models, "_get_session_save_publication_lock", get_lock_then_rewire)
    session = Session(session_id="parent_symlink_rewire", messages=_messages(2))

    with pytest.raises(OSError, match="session sidecar parent changed"):
        session.save(touch_updated_at=False, skip_index=True)

    assert not (real_a / "parent_symlink_rewire.json").exists()
    assert not (real_b / "parent_symlink_rewire.json").exists()


def test_parent_rename_and_replacement_after_binding_fails_closed(tmp_path, monkeypatch):
    store = tmp_path / "sessions"
    moved_store = tmp_path / "sessions-moved"
    store.mkdir()
    monkeypatch.setattr(models, "SESSION_DIR", store)
    monkeypatch.setattr(models, "SESSION_INDEX_FILE", store / "_index.json")
    original_get = models._get_session_save_publication_lock
    swapped = False

    def get_lock_then_replace_parent(path):
        nonlocal swapped
        lock = original_get(path)
        if not swapped:
            swapped = True
            store.rename(moved_store)
            store.mkdir()
        return lock

    monkeypatch.setattr(models, "_get_session_save_publication_lock", get_lock_then_replace_parent)
    session = Session(session_id="parent_replaced", messages=_messages(2))

    with pytest.raises(OSError, match="session sidecar parent changed"):
        session.save(touch_updated_at=False, skip_index=True)

    assert not (moved_store / "parent_replaced.json").exists()
    assert not (store / "parent_replaced.json").exists()


def test_final_symlink_swap_after_binding_fails_closed(tmp_path, monkeypatch):
    store = tmp_path / "sessions"
    store.mkdir()
    monkeypatch.setattr(models, "SESSION_DIR", store)
    monkeypatch.setattr(models, "SESSION_INDEX_FILE", store / "_index.json")
    attacker_target = tmp_path / "attacker.json"
    attacker_bytes = json.dumps({"messages": _messages(4)}, indent=2).encode("utf-8")
    attacker_target.write_bytes(attacker_bytes)
    sidecar = store / "final_symlink_swap.json"
    original_get = models._get_session_save_publication_lock
    swapped = False

    def get_lock_then_swap_final(path):
        nonlocal swapped
        lock = original_get(path)
        if not swapped:
            swapped = True
            try:
                sidecar.symlink_to(attacker_target)
            except OSError as exc:  # pragma: no cover - platform/filesystem dependent
                pytest.skip(f"file symlinks unavailable: {exc}")
        return lock

    monkeypatch.setattr(models, "_get_session_save_publication_lock", get_lock_then_swap_final)
    session = Session(session_id="final_symlink_swap", messages=_messages(2))

    with pytest.raises(OSError, match="refusing to follow final symlink"):
        session.save(touch_updated_at=False, skip_index=True)

    assert sidecar.is_symlink()
    assert attacker_target.read_bytes() == attacker_bytes
    assert not sidecar.with_suffix(".json.bak").exists()


def test_session_dir_rewire_cannot_split_source_and_cache_targets(tmp_path, monkeypatch):
    store_a = tmp_path / "store-a"
    store_b = tmp_path / "store-b"
    store_a.mkdir()
    store_b.mkdir()
    monkeypatch.setattr(models, "SESSION_DIR", store_a)
    monkeypatch.setattr(models, "SESSION_INDEX_FILE", store_a / "_index.json")
    monkeypatch.setattr(models, "_SESSION_TAIL_CACHE_MIN_SOURCE_BYTES", 0)
    original_get = models._get_session_save_publication_lock
    rewired = False

    def get_lock_then_rewire_profile(path):
        nonlocal rewired
        lock = original_get(path)
        if not rewired:
            rewired = True
            monkeypatch.setattr(models, "SESSION_DIR", store_b)
            monkeypatch.setattr(models, "SESSION_INDEX_FILE", store_b / "_index.json")
        return lock

    monkeypatch.setattr(models, "_get_session_save_publication_lock", get_lock_then_rewire_profile)
    session = Session(session_id="profile_target_binding", messages=_messages(2))

    session.save(touch_updated_at=False, skip_index=True)

    assert (store_a / "profile_target_binding.json").exists()
    assert (store_a / "_tail_cache" / "v1" / "profile_target_binding.json").exists()
    assert not (store_b / "profile_target_binding.json").exists()
    assert not (store_b / "_tail_cache" / "v1" / "profile_target_binding.json").exists()
