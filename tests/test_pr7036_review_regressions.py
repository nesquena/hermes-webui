"""Maintainer re-gate: malformed compression parents and post-replace failures."""
import json
from pathlib import Path

import pytest

from api import models, session_discoverability, session_recovery, streaming


@pytest.fixture
def store(tmp_path, monkeypatch):
    directory = tmp_path / "sessions"
    directory.mkdir()
    monkeypatch.setattr(models, "SESSION_DIR", directory)
    monkeypatch.setattr(models, "SESSION_INDEX_FILE", directory / "_index.json")
    monkeypatch.setattr(streaming, "SESSION_DIR", directory)
    monkeypatch.setattr(models, "SESSIONS", {})
    return directory


def test_malformed_compression_parent_is_archived_and_restored(store):
    parent = models.Session(session_id="parent", messages=[{"role": "user", "content": "old"}])
    parent.save(skip_index=True)
    malformed = b'{"session_id": "parent", broken'
    parent.path.write_bytes(malformed)
    child = models.Session(
        session_id="child", parent_session_id="parent",
        messages=[{"role": "user", "content": "recoverable history"}],
    )

    streaming._preserve_pre_compression_snapshot(child, "parent")

    loaded = models.Session.load("parent")
    assert loaded is not None
    assert loaded.pre_compression_snapshot is True
    assert loaded.messages == child.messages
    assert loaded.parent_session_id is None  # no self-referential lineage
    archives = list(store.glob("parent.json.bak.archive-*"))
    assert len(archives) == 1
    assert archives[0].read_bytes() == malformed
    assert child.session_id == "child"


def test_replace_fsync_failure_adopts_exact_visible_revision(store, monkeypatch):
    session = models.Session(session_id="owner", messages=[{"role": "user", "content": "old"}])
    session.save(skip_index=True)
    session.messages.append({"role": "assistant", "content": "new"})
    real_fsync = models._fsync_sidecar_directory
    fail = True

    def fail_once(directory):
        nonlocal fail
        if fail:
            fail = False
            raise OSError("injected post-replace fsync failure")
        return real_fsync(directory)

    monkeypatch.setattr(models, "_fsync_sidecar_directory", fail_once)
    with pytest.raises(OSError, match="post-replace"):
        session.save(skip_index=True)
    assert [m["content"] for m in models.Session.load("owner").messages] == ["old", "new"]
    session.save(skip_index=True)
    assert [m["content"] for m in models.Session.load("owner").messages] == ["old", "new"]


def test_replace_fsync_failure_foreign_visible_revision_invalidates(store, monkeypatch):
    session = models.Session(session_id="owner", messages=[{"role": "user", "content": "old"}])
    session.save(skip_index=True)
    models.SESSIONS["owner"] = session

    def foreign_after_replace(directory):
        raw = json.loads(session.path.read_text(encoding="utf-8"))
        raw["messages"].append({"role": "assistant", "content": "foreign"})
        session.path.write_text(json.dumps(raw), encoding="utf-8")
        raise OSError("injected foreign publication")

    monkeypatch.setattr(models, "_fsync_sidecar_directory", foreign_after_replace)
    session.messages.append({"role": "assistant", "content": "ours"})
    with pytest.raises(OSError, match="foreign publication"):
        session.save(skip_index=True)
    assert "owner" not in models.SESSIONS
    with pytest.raises(models.StaleSessionGenerationError):
        session.save(skip_index=True)
    assert models.Session.load("owner").messages[-1]["content"] == "foreign"


def test_flag_repair_post_replace_failure_evicts_cached_owner(store, tmp_path, monkeypatch):
    session = models.Session(session_id="repair", messages=[{"role": "user", "content": "kept"}])
    session.is_cli_session = True
    session.source_tag = "webui"
    session.save(skip_index=True)
    models.SESSIONS["repair"] = session
    monkeypatch.setattr(models, "_fsync_sidecar_directory", lambda _: (_ for _ in ()).throw(OSError("fsync repair")))
    with pytest.raises(OSError, match="fsync repair"):
        session_discoverability._clear_sidecar_cli_flag(store, "repair", tmp_path / "backup", {})
    assert json.loads(session.path.read_text(encoding="utf-8"))["is_cli_session"] is False
    assert "repair" not in models.SESSIONS
    with pytest.raises(models.StaleSessionGenerationError):
        session.save(skip_index=True)


def test_backup_recovery_post_replace_failure_evicts_cached_owner(store, monkeypatch):
    session = models.Session(session_id="recover", messages=[{"role": "user", "content": "old"}])
    session.save(skip_index=True)
    session.messages.append({"role": "assistant", "content": "lost if shrunk"})
    session.save(skip_index=True)
    session.messages = [{"role": "user", "content": "old"}]
    session.save(skip_index=True)
    models.SESSIONS["recover"] = session
    monkeypatch.setattr(models, "_fsync_sidecar_directory", lambda _: (_ for _ in ()).throw(OSError("fsync recovery")))
    result = session_recovery.recover_session(session.path)
    assert result["restored"] is False
    assert "recover" not in models.SESSIONS
    assert len(json.loads(session.path.read_text(encoding="utf-8"))["messages"]) == 2
    with pytest.raises(models.StaleSessionGenerationError):
        session.save(skip_index=True)


def test_large_growing_save_streams_exact_revision_without_full_json_parse(store, monkeypatch):
    session = models.Session(
        session_id="large", messages=[{"role": "user", "content": "x" * (8 * 1024 * 1024)}],
    )
    session.save(skip_index=True)
    real_loads = models.json.loads
    real_read_bytes = Path.read_bytes
    parse_sizes = []
    full_reads = []

    def tracking_loads(data, *args, **kwargs):
        parse_sizes.append(len(data))
        return real_loads(data, *args, **kwargs)

    def tracking_bytes(path, *args, **kwargs):
        if path == session.path:
            full_reads.append(path)
        return real_read_bytes(path, *args, **kwargs)

    monkeypatch.setattr(models.json, "loads", tracking_loads)
    monkeypatch.setattr(Path, "read_bytes", tracking_bytes)
    session.messages.append({"role": "assistant", "content": "next"})
    session.save(skip_index=True)
    assert full_reads == []
    assert parse_sizes and max(parse_sizes) < 1024 * 1024
    assert session.path.with_suffix(".json.bak").exists() is False


def test_same_generation_body_rewrite_is_still_fenced(store):
    session = models.Session(
        session_id="body_rewrite", messages=[{"role": "user", "content": "aaa"}],
    )
    session.save(skip_index=True)
    before = session.path.read_bytes()
    session.path.write_bytes(before.replace(b"aaa", b"bbb"))
    with pytest.raises(models.StaleSessionGenerationError):
        session.save(skip_index=True)
    assert b"bbb" in session.path.read_bytes()
