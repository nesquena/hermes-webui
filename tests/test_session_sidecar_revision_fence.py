import builtins
import errno
import hashlib
import json
import multiprocessing
import os
import sqlite3
from contextlib import contextmanager
from pathlib import Path

import pytest


def _patch_store(monkeypatch, models, session_dir: Path) -> None:
    session_dir.mkdir(parents=True, exist_ok=True)
    monkeypatch.setattr(models, "SESSION_DIR", session_dir)
    monkeypatch.setattr(models, "SESSION_INDEX_FILE", session_dir / "_index.json")
    with models.LOCK:
        models.SESSIONS.clear()


def _process_writer(
    session_dir, sid, marker, start_event, ready_queue, result_queue
):
    from api import models

    models.SESSION_DIR = Path(session_dir)
    models.SESSION_INDEX_FILE = Path(session_dir) / "_index.json"
    session = models.Session.load(sid)
    assert session is not None
    session.messages.append({"role": "assistant", "content": marker})
    ready_queue.put(marker)
    start_event.wait(timeout=15)
    try:
        session.save(skip_index=True)
    except models.StaleSessionGenerationError:
        result_queue.put((marker, "stale"))
    else:
        result_queue.put((marker, "saved"))


def test_stale_loaded_instance_cannot_overwrite_newer_sidecar(
    tmp_path, monkeypatch
):
    from api import models

    session_dir = tmp_path / "sessions"
    _patch_store(monkeypatch, models, session_dir)
    sid = "revision-cas"
    seed = models.Session(
        session_id=sid,
        workspace=str(tmp_path),
        messages=[{"role": "user", "content": "seed"}],
    )
    seed.save(skip_index=True)
    first = models.Session.load(sid)
    with models.LOCK:
        models.SESSIONS.pop(sid, None)
    second = models.Session.load(sid)
    assert first is not None and second is not None

    second.messages.append({"role": "assistant", "content": "newer"})
    second.save(skip_index=True)
    first.title = "stale mutation"
    with pytest.raises(models.StaleSessionGenerationError):
        first.save(skip_index=True)

    persisted = json.loads(
        (session_dir / f"{sid}.json").read_text(encoding="utf-8")
    )
    assert persisted["messages"][-1]["content"] == "newer"


def test_native_windows_newlines_do_not_invalidate_second_save(
    tmp_path, monkeypatch
):
    from api import models

    session_dir = tmp_path / "sessions"
    _patch_store(monkeypatch, models, session_dir)
    sid = "windows-newline-revision"
    real_open = builtins.open

    def windows_text_open(file, mode="r", *args, **kwargs):
        if mode == "w" and ".tmp." in str(file) and kwargs.get("newline") is None:
            kwargs["newline"] = "\r\n"
        return real_open(file, mode, *args, **kwargs)

    monkeypatch.setattr(builtins, "open", windows_text_open)
    session = models.Session(
        session_id=sid,
        workspace=str(tmp_path),
        messages=[{"role": "user", "content": "first"}],
    )
    session.save(skip_index=True)
    session.messages.append({"role": "assistant", "content": "second"})

    session.save(skip_index=True)

    persisted = json.loads(
        (session_dir / f"{sid}.json").read_text(encoding="utf-8")
    )
    assert persisted["_sidecar_generation_v1"] == 2
    assert persisted["messages"][-1]["content"] == "second"


def test_new_instance_expected_absent_never_overwrites_existing_sid(
    tmp_path, monkeypatch
):
    from api import models

    session_dir = tmp_path / "sessions"
    _patch_store(monkeypatch, models, session_dir)
    sid = "create-only"
    first = models.Session(
        session_id=sid,
        workspace=str(tmp_path),
        messages=[{"role": "user", "content": "first"}],
    )
    stale = models.Session(
        session_id=sid,
        workspace=str(tmp_path),
        messages=[{"role": "user", "content": "stale"}],
    )

    first.save(skip_index=True)
    with pytest.raises(models.StaleSessionGenerationError):
        stale.save(skip_index=True)
    persisted = json.loads(
        (session_dir / f"{sid}.json").read_text(encoding="utf-8")
    )
    assert persisted["messages"] == first.messages


def test_create_only_publish_fails_closed_without_atomic_primitive(
    tmp_path, monkeypatch
):
    from api import models

    source = tmp_path / "source.tmp"
    destination = tmp_path / "destination.json"
    source.write_bytes(b'{"complete": true}')

    def unsupported_link(*_args, **_kwargs):
        raise OSError(errno.EXDEV, "hard links unsupported")

    monkeypatch.setattr(models.os, "link", unsupported_link)

    with pytest.raises(OSError, match="hard links unsupported"):
        models._publish_sidecar_no_replace(source, destination)

    assert not destination.exists()
    assert source.read_bytes() == b'{"complete": true}'


def test_generation_is_scoped_per_sid_across_rotation(tmp_path, monkeypatch):
    from api import models

    session_dir = tmp_path / "sessions"
    _patch_store(monkeypatch, models, session_dir)
    old_sid = "parent"
    new_sid = "continuation"
    session = models.Session(
        session_id=old_sid,
        workspace=str(tmp_path),
        messages=[{"role": "user", "content": "parent"}],
    )
    session.save(skip_index=True)

    session.session_id = new_sid
    session.parent_session_id = old_sid
    session.messages.append({"role": "assistant", "content": "continuation"})
    session.save(skip_index=True)
    session.session_id = old_sid
    session.title = "archived parent"
    session.save(skip_index=True)

    parent = json.loads(
        (session_dir / f"{old_sid}.json").read_text(encoding="utf-8")
    )
    continuation = json.loads(
        (session_dir / f"{new_sid}.json").read_text(encoding="utf-8")
    )
    assert parent["_sidecar_generation_v1"] == 2
    assert continuation["_sidecar_generation_v1"] == 1


@pytest.mark.skipif(os.name == "nt", reason="fork-based multiprocess CAS probe")
def test_two_process_writers_have_exactly_one_cas_winner(tmp_path, monkeypatch):
    from api import models

    session_dir = tmp_path / "sessions"
    _patch_store(monkeypatch, models, session_dir)
    sid = "multiprocess-cas"
    models.Session(
        session_id=sid,
        workspace=str(tmp_path),
        messages=[{"role": "user", "content": "base"}],
    ).save(skip_index=True)

    context = multiprocessing.get_context("fork")
    start_event = context.Event()
    ready_queue = context.Queue()
    result_queue = context.Queue()
    processes = [
        context.Process(
            target=_process_writer,
            args=(
                session_dir,
                sid,
                marker,
                start_event,
                ready_queue,
                result_queue,
            ),
        )
        for marker in ("writer-a", "writer-b")
    ]
    for process in processes:
        process.start()
    assert {ready_queue.get(timeout=15), ready_queue.get(timeout=15)} == {
        "writer-a",
        "writer-b",
    }
    start_event.set()
    for process in processes:
        process.join(timeout=20)
        assert process.exitcode == 0

    results = dict(result_queue.get(timeout=5) for _ in processes)
    assert sorted(results.values()) == ["saved", "stale"]
    persisted = json.loads(
        (session_dir / f"{sid}.json").read_text(encoding="utf-8")
    )
    committed = {
        message.get("content")
        for message in persisted["messages"]
        if message.get("content") in {"writer-a", "writer-b"}
    }
    assert len(committed) == 1
    assert persisted["_sidecar_generation_v1"] == 2


def test_recovery_expected_absent_uses_create_or_fail(tmp_path, monkeypatch):
    from api import session_recovery

    session_path = tmp_path / "absent.json"
    backup_path = session_path.with_suffix(".json.bak")
    backup_path.write_text(
        json.dumps(
            {
                "session_id": "absent",
                "messages": [{"role": "user", "content": "backup"}],
            }
        ),
        encoding="utf-8",
    )
    competing = {
        "messages": [{"role": "user", "content": "competing"}]
    }
    real_link = session_recovery.os.link

    def competing_link(src, dst):
        Path(dst).write_text(json.dumps(competing), encoding="utf-8")
        return real_link(src, dst)

    monkeypatch.setattr(session_recovery.os, "link", competing_link)
    result = session_recovery.recover_session(session_path)
    assert result["restored"] is False
    assert result["stale_generation"] is True
    assert json.loads(session_path.read_text(encoding="utf-8")) == competing


def test_recovery_invalidates_cached_alias_and_publishes_generation(
    tmp_path, monkeypatch
):
    from api import models, session_recovery

    session_dir = tmp_path / "sessions"
    _patch_store(monkeypatch, models, session_dir)
    sid = "recovery-alias"
    session_path = session_dir / f"{sid}.json"
    backup_path = session_path.with_suffix(".json.bak")
    live = {
        "session_id": sid,
        "workspace": str(tmp_path),
        "messages": [{"role": "user", "content": "live"}],
    }
    backup = {
        "session_id": sid,
        "workspace": str(tmp_path),
        "messages": [
            {"role": "user", "content": "one"},
            {"role": "assistant", "content": "two"},
            {"role": "user", "content": "three"},
        ],
    }
    session_path.write_text(json.dumps(live), encoding="utf-8")
    backup_path.write_text(json.dumps(backup), encoding="utf-8")
    alias = models.Session.load(sid)
    assert alias is not None
    with models.LOCK:
        models.SESSIONS[sid] = alias
    fsynced = []
    monkeypatch.setattr(
        models,
        "_fsync_sidecar_directory",
        lambda directory: fsynced.append(Path(directory)),
    )

    result = session_recovery.recover_session(session_path)
    assert result["restored"] is True
    assert fsynced == [session_dir]
    with models.LOCK:
        assert sid not in models.SESSIONS
    restored = json.loads(session_path.read_text(encoding="utf-8"))
    assert restored["_sidecar_generation_v1"] == 1
    alias.title = "stale alias"
    with pytest.raises(RuntimeError, match="stale|generation"):
        alias.save(skip_index=True)


def test_backup_is_monotone_across_later_poorer_shrinks(tmp_path, monkeypatch):
    from api import models

    session_dir = tmp_path / "sessions"
    _patch_store(monkeypatch, models, session_dir)
    sid = "monotone-backup"
    session = models.Session(
        session_id=sid,
        workspace=str(tmp_path),
        messages=[
            {"role": "user", "content": str(index)} for index in range(10)
        ],
    )
    session.save(skip_index=True)
    session.messages = session.messages[:2]
    session.save(skip_index=True)
    session.messages = [
        {"role": "user", "content": str(index)} for index in range(5)
    ]
    session.save(skip_index=True)
    session.messages = session.messages[:4]
    session.save(skip_index=True)
    backup = json.loads(
        (session_dir / f"{sid}.json.bak").read_text(encoding="utf-8")
    )
    assert len(backup["messages"]) == 10


def test_backup_retirement_requires_matching_receipt(tmp_path, monkeypatch):
    from api import models

    session_dir = tmp_path / "sessions"
    _patch_store(monkeypatch, models, session_dir)
    sid = "cleanup"
    backup_path = session_dir / f"{sid}.json.bak"
    live_path = session_dir / f"{sid}.json"
    live_path.write_text("committed live", encoding="utf-8")
    live_receipt = models._read_sidecar_revision(live_path, sid)
    backup_path.write_text("first backup", encoding="utf-8")

    first_receipt = models._read_sidecar_revision(backup_path, sid)
    backup_path.write_text("newer foreign backup", encoding="utf-8")

    assert models._retire_backup_if_owned(
        sid, backup_path, None, live_receipt
    ) is False
    assert (
        models._retire_backup_if_owned(
            sid, backup_path, first_receipt, live_receipt
        )
        is False
    )
    assert backup_path.read_text(encoding="utf-8") == "newer foreign backup"

    current_receipt = models._read_sidecar_revision(backup_path, sid)
    archive_path = backup_path.with_name(f"{backup_path.name}.archive-test")
    archive_path.write_text('{"archived": true}', encoding="utf-8")
    assert (
        models._retire_backup_if_owned(
            sid, backup_path, current_receipt, live_receipt
        )
        is True
    )
    assert not backup_path.exists()
    assert not archive_path.exists()


def test_shrinking_save_fails_closed_when_backup_publish_fails(
    tmp_path, monkeypatch
):
    from api import models

    session_dir = tmp_path / "sessions"
    _patch_store(monkeypatch, models, session_dir)
    sid = "backup-fail-closed"
    session = models.Session(
        session_id=sid,
        workspace=str(tmp_path),
        messages=[
            {"role": "user", "content": "one"},
            {"role": "assistant", "content": "two"},
            {"role": "user", "content": "three"},
        ],
    )
    session.save(skip_index=True)
    original = json.loads(session.path.read_text(encoding="utf-8"))
    real_replace = models._safe_replace

    def fail_backup_publish(source, destination):
        if Path(destination).suffix == ".bak":
            raise OSError("simulated backup publish failure")
        return real_replace(source, destination)

    monkeypatch.setattr(models, "_safe_replace", fail_backup_publish)
    session.messages = session.messages[:1]
    with pytest.raises(RuntimeError, match="backup"):
        session.save(skip_index=True)
    persisted = json.loads(session.path.read_text(encoding="utf-8"))
    assert persisted["messages"] == original["messages"]
    assert (
        persisted["_sidecar_generation_v1"]
        == original["_sidecar_generation_v1"]
    )


def test_malformed_backup_is_archived_before_live_snapshot_promotion(
    tmp_path, monkeypatch
):
    from api import models

    session_dir = tmp_path / "sessions"
    _patch_store(monkeypatch, models, session_dir)
    sid = "malformed-backup-recovery"
    session = models.Session(
        session_id=sid,
        workspace=str(tmp_path),
        messages=[
            {"role": "user", "content": "one"},
            {"role": "assistant", "content": "two"},
            {"role": "user", "content": "three"},
        ],
    )
    session.save(skip_index=True)
    before_shrink = session.path.read_bytes()
    malformed = b'{"broken":'
    backup_path = session.path.with_suffix(".json.bak")
    backup_path.write_bytes(malformed)
    session.messages = session.messages[:1]

    session.save(skip_index=True)

    archive = backup_path.with_name(
        f"{backup_path.name}.archive-{hashlib.sha256(malformed).hexdigest()}"
    )
    assert archive.read_bytes() == malformed
    assert backup_path.read_bytes() == before_shrink
    assert len(json.loads(session.path.read_text(encoding="utf-8"))["messages"]) == 1


def test_malformed_backup_archive_does_not_require_hard_links(
    tmp_path, monkeypatch
):
    from api import models

    session_dir = tmp_path / "sessions"
    _patch_store(monkeypatch, models, session_dir)
    sid = "malformed-backup-no-hardlinks"
    session = models.Session(
        session_id=sid,
        workspace=str(tmp_path),
        messages=[
            {"role": "user", "content": "one"},
            {"role": "assistant", "content": "two"},
            {"role": "user", "content": "three"},
        ],
    )
    session.save(skip_index=True)
    before_shrink = session.path.read_bytes()
    malformed = b'{"broken":'
    backup_path = session.path.with_suffix(".json.bak")
    backup_path.write_bytes(malformed)
    session.messages = session.messages[:1]

    def unsupported_link(*_args, **_kwargs):
        raise OSError(errno.EOPNOTSUPP, "hard links unsupported")

    monkeypatch.setattr(models.os, "link", unsupported_link)

    session.save(skip_index=True)

    archive = backup_path.with_name(
        f"{backup_path.name}.archive-{hashlib.sha256(malformed).hexdigest()}"
    )
    assert archive.read_bytes() == malformed
    assert backup_path.read_bytes() == before_shrink
    assert len(json.loads(session.path.read_text(encoding="utf-8"))["messages"]) == 1


def test_archive_temporary_name_handles_maximum_valid_session_id(tmp_path):
    from api import models

    sid = "s" * 150
    backup_path = tmp_path / f"{sid}.json.bak"
    backup_path.write_bytes(b'{"broken":')
    receipt = models._read_sidecar_revision(backup_path, sid)

    archive_path = models._archive_incomparable_backup(
        sid,
        backup_path,
        receipt,
    )

    assert archive_path.read_bytes() == b'{"broken":'
    assert len(os.fsencode(archive_path.name)) <= 255


@pytest.mark.skipif(os.name == "nt", reason="POSIX directory fsync contract")
def test_first_sidecar_publication_fsyncs_parent_directory(tmp_path, monkeypatch):
    from api import models

    session_dir = tmp_path / "sessions"
    _patch_store(monkeypatch, models, session_dir)
    fsynced = []
    monkeypatch.setattr(
        models,
        "_fsync_sidecar_directory",
        lambda directory: fsynced.append(Path(directory)),
        raising=False,
    )
    session = models.Session(
        session_id="durable-first-publication",
        workspace=str(tmp_path),
        messages=[{"role": "user", "content": "first"}],
    )

    session.save(skip_index=True)

    assert fsynced == [session_dir]


@pytest.mark.skipif(os.name == "nt", reason="POSIX directory fsync contract")
def test_shrink_publications_fsync_archive_backup_then_live(
    tmp_path,
    monkeypatch,
):
    from api import models

    session_dir = tmp_path / "sessions"
    _patch_store(monkeypatch, models, session_dir)
    session = models.Session(
        session_id="durable-shrink-publication",
        workspace=str(tmp_path),
        messages=[
            {"role": "user", "content": "one"},
            {"role": "assistant", "content": "two"},
            {"role": "user", "content": "three"},
        ],
    )
    session.save(skip_index=True)
    backup_path = session.path.with_suffix(".json.bak")
    malformed = b'{"broken":'
    backup_path.write_bytes(malformed)
    session.messages = session.messages[:1]
    events = []
    real_replace = models._safe_replace

    def record_replace(source, destination):
        events.append(("replace", Path(destination).name))
        return real_replace(source, destination)

    monkeypatch.setattr(models, "_safe_replace", record_replace)
    monkeypatch.setattr(
        models,
        "_fsync_sidecar_directory",
        lambda directory: events.append(("fsync", Path(directory).name)),
        raising=False,
    )

    session.save(skip_index=True)

    archive_name = (
        f"{backup_path.name}.archive-{hashlib.sha256(malformed).hexdigest()}"
    )
    assert events == [
        ("replace", archive_name),
        ("fsync", session_dir.name),
        ("replace", backup_path.name),
        ("fsync", session_dir.name),
        ("replace", session.path.name),
        ("fsync", session_dir.name),
    ]


def test_non_object_backup_is_archived_before_live_snapshot_promotion(
    tmp_path, monkeypatch
):
    from api import models

    session_dir = tmp_path / "sessions"
    _patch_store(monkeypatch, models, session_dir)
    sid = "non-object-backup-recovery"
    session = models.Session(
        session_id=sid,
        workspace=str(tmp_path),
        messages=[
            {"role": "user", "content": "one"},
            {"role": "assistant", "content": "two"},
        ],
    )
    session.save(skip_index=True)
    before_shrink = session.path.read_bytes()
    unusable = b"[]"
    backup_path = session.path.with_suffix(".json.bak")
    backup_path.write_bytes(unusable)
    session.messages = session.messages[:1]

    session.save(skip_index=True)

    archive = backup_path.with_name(
        f"{backup_path.name}.archive-{hashlib.sha256(unusable).hexdigest()}"
    )
    assert archive.read_bytes() == unusable
    assert backup_path.read_bytes() == before_shrink


def test_foreign_sid_backup_is_archived_and_replaced_before_shrink(
    tmp_path, monkeypatch
):
    from api import models, session_recovery

    session_dir = tmp_path / "sessions"
    _patch_store(monkeypatch, models, session_dir)
    sid = "foreign-backup-owner"
    messages = [
        {"role": "user", "content": "one"},
        {"role": "assistant", "content": "two"},
        {"role": "user", "content": "three"},
    ]
    session = models.Session(
        session_id=sid,
        workspace=str(tmp_path),
        messages=messages,
    )
    session.save(skip_index=True)
    foreign_bytes = json.dumps(
        {"session_id": "foreign-owner", "messages": messages},
        ensure_ascii=False,
    ).encode("utf-8")
    backup_path = session.path.with_suffix(".json.bak")
    backup_path.write_bytes(foreign_bytes)
    session.messages = messages[:1]

    session.save(skip_index=True)

    archive = backup_path.with_name(
        f"{backup_path.name}.archive-{hashlib.sha256(foreign_bytes).hexdigest()}"
    )
    assert archive.read_bytes() == foreign_bytes
    primary_backup = json.loads(backup_path.read_text(encoding="utf-8"))
    assert primary_backup["session_id"] == sid
    result = session_recovery.recover_session(session.path)
    assert result["restored"] is True
    restored = json.loads(session.path.read_text(encoding="utf-8"))
    assert restored["messages"] == messages


def test_workspace_patch_does_not_grant_stale_alias_new_revision(
    tmp_path, monkeypatch
):
    from api import models

    session_dir = tmp_path / "sessions"
    _patch_store(monkeypatch, models, session_dir)
    sid = "workspace-stale-alias"
    original_workspace = tmp_path / "before"
    recovered_workspace = tmp_path / "after"
    seed = models.Session(
        session_id=sid,
        workspace=str(original_workspace),
        messages=[{"role": "user", "content": "seed"}],
    )
    seed.save(skip_index=True)
    stale = models.Session.load(sid)
    newer = models.Session.load(sid)
    assert stale is not None and newer is not None
    newer.messages.append({"role": "assistant", "content": "newer"})
    newer.save(skip_index=True)
    with models.LOCK:
        models.SESSIONS[sid] = stale

    current = models.persist_recovered_workspace_binding(
        stale,
        recovered_workspace,
        expected_workspace=str(original_workspace.resolve()),
    )

    assert current is not stale
    assert current.messages[-1]["content"] == "newer"
    assert current.workspace == str(recovered_workspace.resolve())
    stale.title = "must not own current revision"
    with pytest.raises(models.StaleSessionGenerationError):
        stale.save(skip_index=True)
    persisted = json.loads(seed.path.read_text(encoding="utf-8"))
    assert persisted["messages"][-1]["content"] == "newer"


def test_incomparable_backup_is_archived_before_latest_snapshot_promotion(
    tmp_path, monkeypatch
):
    from api import models

    session_dir = tmp_path / "sessions"
    _patch_store(monkeypatch, models, session_dir)
    sid = "incomparable-backup"
    a = {"role": "user", "content": "A"}
    unique = {"role": "assistant", "content": "UNIQUE-U"}
    session = models.Session(
        session_id=sid,
        workspace=str(tmp_path),
        messages=[a, unique],
    )
    session.save(skip_index=True)
    session.messages = [a]
    session.save(skip_index=True)
    session.messages = [
        a,
        {"role": "assistant", "content": "D1"},
        {"role": "user", "content": "D2"},
    ]
    session.save(skip_index=True)

    session.messages = [a]
    session.save(skip_index=True)

    live = json.loads(session.path.read_text(encoding="utf-8"))
    backup_path = session.path.with_suffix(".json.bak")
    backup = json.loads(backup_path.read_text(encoding="utf-8"))
    archives = list(session_dir.glob(f"{backup_path.name}.archive-*"))
    assert [row["content"] for row in live["messages"]] == ["A"]
    assert [row["content"] for row in backup["messages"]] == ["A", "D1", "D2"]
    assert len(archives) == 1
    archived = json.loads(archives[0].read_text(encoding="utf-8"))
    assert [row["content"] for row in archived["messages"]] == ["A", "UNIQUE-U"]


def test_backup_dominance_preserves_message_order():
    from api.models import _message_rows_cover

    first = {"role": "user", "content": "first"}
    second = {"role": "assistant", "content": "second"}

    assert _message_rows_cover([first, second], [first]) is True
    assert _message_rows_cover([second, first], [first, second]) is False


def test_backup_retirement_requires_live_revision_to_still_match(
    tmp_path, monkeypatch
):
    from api import models

    session_dir = tmp_path / "sessions"
    _patch_store(monkeypatch, models, session_dir)
    sid = "cleanup-live-race"
    session = models.Session(
        session_id=sid,
        workspace=str(tmp_path),
        messages=[
            {"role": "user", "content": "one"},
            {"role": "assistant", "content": "two"},
        ],
    )
    session.save(skip_index=True)
    session.messages = session.messages[:1]
    backup_receipt = session.save(skip_index=True)
    committed_receipt = models._read_sidecar_revision(session.path, sid)
    session.messages.append({"role": "assistant", "content": "new generation"})
    session.save(skip_index=True)

    assert models._retire_backup_if_owned(
        sid,
        session.path.with_suffix(".json.bak"),
        backup_receipt,
        committed_receipt,
    ) is False
    assert session.path.with_suffix(".json.bak").exists()


def test_same_count_external_metadata_update_reloads_cached_owner(
    tmp_path, monkeypatch
):
    from api import models

    session_dir = tmp_path / "sessions"
    _patch_store(monkeypatch, models, session_dir)
    sid = "same-count-metadata"
    seed = models.Session(
        session_id=sid,
        title="before",
        workspace=str(tmp_path),
        messages=[{"role": "user", "content": "same transcript"}],
    )
    seed.save(skip_index=True)
    cached = models.Session.load(sid)
    external = models.Session.load(sid)
    assert cached is not None and external is not None
    with models.LOCK:
        models.SESSIONS[sid] = cached
    external.title = "after"
    external.save(skip_index=True)

    loaded = models.get_session(sid)

    assert loaded is not cached
    assert loaded.title == "after"
    assert loaded.messages == cached.messages


def test_modern_cache_freshness_uses_prefix_generation_not_full_digest(
    tmp_path, monkeypatch
):
    from api import models

    session_dir = tmp_path / "sessions"
    _patch_store(monkeypatch, models, session_dir)
    session = models.Session(
        session_id="prefix-generation",
        workspace=str(tmp_path),
        messages=[{"role": "user", "content": "large sidecar proxy"}],
    )
    session.save(skip_index=True)
    cached = models.Session.load(session.session_id)
    assert cached is not None

    monkeypatch.setattr(
        models,
        "_read_sidecar_revision",
        lambda *_args, **_kwargs: pytest.fail("cache hit hashed the full sidecar"),
    )

    assert models._cached_session_lags_disk(cached) is False


def test_recovery_rejects_backup_with_foreign_embedded_sid(tmp_path, monkeypatch):
    from api import models, session_recovery

    session_dir = tmp_path / "sessions"
    _patch_store(monkeypatch, models, session_dir)
    sid = "expected-sid"
    session_path = session_dir / f"{sid}.json"
    live = {
        "session_id": sid,
        "messages": [{"role": "user", "content": "live"}],
    }
    foreign = {
        "session_id": "foreign-sid",
        "messages": [
            {"role": "user", "content": "foreign one"},
            {"role": "assistant", "content": "foreign two"},
        ],
    }
    session_path.write_text(json.dumps(live), encoding="utf-8")
    session_path.with_suffix(".json.bak").write_text(
        json.dumps(foreign), encoding="utf-8"
    )

    result = session_recovery.recover_session(session_path)

    assert result["restored"] is False
    assert "session id" in result["error"].lower()
    assert json.loads(session_path.read_text(encoding="utf-8")) == live


def test_create_only_publish_uses_native_windows_rename_without_hardlinks(
    tmp_path, monkeypatch
):
    from api import models

    source = tmp_path / "source.tmp"
    destination = tmp_path / "destination.json"
    source.write_bytes(b'{"complete": true}')

    def unsupported_link(_source, _destination):
        raise OSError(errno.EOPNOTSUPP, "hard links unsupported")

    real_rename = models.os.rename
    rename_calls = []

    def create_only_rename(rename_source, rename_destination):
        rename_calls.append((rename_source, rename_destination))
        assert not destination.exists()
        real_rename(rename_source, rename_destination)

    monkeypatch.setattr(models.os, "link", unsupported_link)
    monkeypatch.setattr(models.os, "rename", create_only_rename)
    monkeypatch.setattr(models.os, "name", "nt")

    models._publish_sidecar_no_replace(source, destination)

    assert rename_calls == [(source, destination)]
    assert destination.read_bytes() == b'{"complete": true}'
    assert not source.exists()


def test_orphan_recovery_rechecks_delete_tombstone_under_authority(
    tmp_path, monkeypatch
):
    from api import models, session_recovery

    session_dir = tmp_path / "sessions"
    _patch_store(monkeypatch, models, session_dir)
    sid = "deleted-before-recovery"
    session_path = session_dir / f"{sid}.json"
    session_path.with_suffix(".json.bak").write_text(
        json.dumps(
            {
                "session_id": sid,
                "messages": [{"role": "user", "content": "deleted"}],
            }
        ),
        encoding="utf-8",
    )
    models._record_webui_deleted_session_tombstone(sid)

    result = session_recovery.recover_session(session_path)

    assert result["restored"] is False
    assert result.get("deleted") is True
    assert not session_path.exists()


def test_state_db_materialization_rechecks_delete_inside_authority(
    tmp_path, monkeypatch
):
    from api import models, session_recovery

    session_dir = tmp_path / "sessions"
    _patch_store(monkeypatch, models, session_dir)
    sid = "deleted-during-reconcile"
    db_path = tmp_path / "state.db"
    with sqlite3.connect(db_path) as conn:
        conn.execute(
            "CREATE TABLE sessions (id TEXT PRIMARY KEY, source TEXT, title TEXT, "
            "model TEXT, started_at REAL, message_count INTEGER)"
        )
        conn.execute(
            "CREATE TABLE messages (id INTEGER PRIMARY KEY, session_id TEXT, "
            "role TEXT, content TEXT, timestamp REAL)"
        )
        conn.execute(
            "INSERT INTO sessions VALUES (?, 'webui', 'deleted', 'model', 1, 1)",
            (sid,),
        )
        conn.execute(
            "INSERT INTO messages VALUES (1, ?, 'user', 'deleted', 1)",
            (sid,),
        )

    real_authority = models._session_sidecar_authority

    @contextmanager
    def delete_before_authority_yields(session_id, *, session_dir=None):
        with sqlite3.connect(db_path) as conn:
            conn.execute("DELETE FROM sessions WHERE id = ?", (session_id,))
        models._record_webui_deleted_session_tombstone(session_id)
        with real_authority(session_id, session_dir=session_dir):
            yield

    monkeypatch.setattr(
        models, "_session_sidecar_authority", delete_before_authority_yields
    )

    result = session_recovery.recover_missing_sidecars_from_state_db(
        session_dir, db_path
    )

    assert result["materialized"] == 0
    assert not (session_dir / f"{sid}.json").exists()
