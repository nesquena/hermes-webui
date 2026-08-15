import hashlib
import io
import json
import os
import stat
import subprocess
import sys
import threading
from pathlib import Path

import pytest


def _row(message_id="assistant-a"):
    return {
        "role": "assistant",
        "content": "",
        "id": message_id,
        "finish_reason": "stop",
        "reasoning": "replayed",
        "timestamp": 123,
    }


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _run_with_isolated_rss(script: Path, *args, timeout: int):
    """Fork the measured CLI from a small launcher, not the full pytest RSS."""
    launcher = (
        "import subprocess,sys; "
        "raise SystemExit(subprocess.call(sys.argv[1:]))"
    )
    return subprocess.run(
        [sys.executable, "-c", launcher, sys.executable, str(script), *map(str, args)],
        check=False,
        capture_output=True,
        text=True,
        timeout=timeout,
    )


def test_large_session_load_defers_inline_repair_and_blocks_save(tmp_path, monkeypatch):
    from api import models

    session_dir = tmp_path / "sessions"
    session_dir.mkdir()
    monkeypatch.setattr(models, "SESSION_DIR", session_dir)
    monkeypatch.setattr(models, "SESSION_INDEX_FILE", session_dir / "_index.json")
    monkeypatch.setattr(models, "_INLINE_REPLAY_REPAIR_MAX_BYTES", 1)
    sid = "offline-repair-required"
    duplicate_partial = {
        "role": "assistant",
        "content": "streaming",
        "_partial": True,
    }
    payload = {
        "session_id": sid,
        "messages": [duplicate_partial, duplicate_partial],
        "context_messages": [],
    }
    sidecar = session_dir / f"{sid}.json"
    sidecar.write_text(json.dumps(payload), encoding="utf-8")

    loaded = models.Session.load(sid)

    assert loaded is not None
    assert loaded.messages == payload["messages"]
    assert loaded.context_messages == payload["context_messages"]
    assert loaded._replay_repair_deferred is True
    assert not sidecar.with_suffix(".json.bak").exists()
    with pytest.raises(RuntimeError, match="offline replay repair"):
        loaded.save(skip_index=True)


def test_offline_compactor_is_atomic_idempotent_and_reversible(tmp_path):
    from scripts.compact_session_replays import compact_sidecar, restore_manifest

    sidecar = tmp_path / "session.json"
    replay = _row()
    unique = {"role": "user", "content": "must survive", "id": "user-a"}
    opaque = {
        "role": "assistant",
        "content": [{"type": "image_url", "image_url": "file:///A.png"}],
        "id": "opaque-a",
    }
    payload = {
        "session_id": "session",
        "_sidecar_generation_v1": 7,
        "message_count": 5,
        "compression_anchor_visible_idx": 4,
        "pre_compression_snapshot": {
            "messages": [replay, replay],
            "note": "copied raw, not recursively reduced",
        },
        "messages": [replay, replay, unique, opaque, opaque],
        "context_messages": [replay, replay, opaque, opaque],
        "tool_calls": [],
    }
    sidecar.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
    original_sha = _sha256(sidecar)

    dry_run = compact_sidecar(sidecar, dry_run=True)
    assert dry_run["status"] == "would_compact"
    assert dry_run["arrays"]["messages"] == {
        "input": 5,
        "output": 4,
        "removed": 1,
    }

    result = compact_sidecar(sidecar)
    repaired_text = sidecar.read_text(encoding="utf-8")
    repaired = json.loads(repaired_text)

    assert result["status"] == "compacted"
    assert repaired["_sidecar_generation_v1"] == 8
    sidecar_epoch = repaired["_sidecar_epoch_v1"]
    assert len(sidecar_epoch) == 32
    assert repaired_text.index('"_sidecar_generation_v1"') < repaired_text.index(
        '"messages"'
    )
    assert repaired["message_count"] == 4
    assert repaired["compression_anchor_visible_idx"] is None
    assert repaired["messages"] == [replay, unique, opaque, opaque]
    assert repaired["context_messages"] == [replay, opaque, opaque]
    assert repaired["pre_compression_snapshot"] == payload["pre_compression_snapshot"]
    assert compact_sidecar(sidecar, dry_run=True)["status"] == "no_change"

    manifest = Path(result["manifest"])
    manifest_payload = json.loads(manifest.read_text(encoding="utf-8"))
    backup = Path(result["backup"])
    assert manifest_payload["source_sha256"] == original_sha == _sha256(backup)
    assert manifest_payload["output_sha256"] == _sha256(sidecar)
    assert manifest_payload["source_generation"] == 7
    assert manifest_payload["output_generation"] == 8
    assert manifest_payload["sidecar_epoch"] == sidecar_epoch

    restored = restore_manifest(manifest)
    assert restored["status"] == "restored"
    restored_payload = json.loads(sidecar.read_text(encoding="utf-8"))
    assert restored_payload.pop("_sidecar_generation_v1") == 9
    assert restored_payload.pop("_sidecar_epoch_v1") == sidecar_epoch
    assert restored["sidecar_epoch"] == sidecar_epoch
    expected_payload = dict(payload)
    expected_payload.pop("_sidecar_generation_v1")
    assert restored_payload == expected_payload
    assert restored["output_sha256"] == _sha256(sidecar)
    assert _sha256(backup) == original_sha


@pytest.mark.skipif(os.name == "nt", reason="offline compactor is POSIX/WSL-only")
def test_compactor_rejects_symlink_source(tmp_path):
    from scripts.compact_session_replays import StreamJSONError, compact_sidecar

    target = tmp_path / "target.json"
    target.write_text(
        json.dumps({
            "session_id": "source-link",
            "messages": [_row(), _row()],
            "context_messages": [],
        }),
        encoding="utf-8",
    )
    source_link = tmp_path / "source-link.json"
    source_link.symlink_to(target)
    original = target.read_bytes()

    with pytest.raises(StreamJSONError, match="source.*symlink"):
        compact_sidecar(source_link)

    assert target.read_bytes() == original
    assert not list(tmp_path.glob("*replay-v10*"))


def test_compactor_requires_embedded_session_id_to_match_filename(tmp_path):
    from scripts.compact_session_replays import StreamJSONError, compact_sidecar

    sidecar = tmp_path / "expected-id.json"
    original = json.dumps({
        "session_id": "different-id",
        "messages": [_row(), _row()],
        "context_messages": [],
    })
    sidecar.write_text(original, encoding="utf-8")

    with pytest.raises(StreamJSONError, match="session_id.*filename"):
        compact_sidecar(sidecar)

    assert sidecar.read_text(encoding="utf-8") == original
    assert not list(tmp_path.glob("*replay-v10*"))


def test_compactor_rejects_session_id_too_long_for_artifact_names(tmp_path):
    from scripts.compact_session_replays import StreamJSONError, compact_sidecar

    session_id = "s" * 151
    sidecar = tmp_path / f"{session_id}.json"
    original = json.dumps({
        "session_id": session_id,
        "messages": [_row(), _row()],
        "context_messages": [],
    })
    sidecar.write_text(original, encoding="utf-8")

    with pytest.raises(StreamJSONError, match="session_id.*150"):
        compact_sidecar(sidecar)

    assert sidecar.read_text(encoding="utf-8") == original
    assert not list(tmp_path.glob("*replay-v10*"))


def test_offline_compactor_uses_shared_partial_identity(tmp_path):
    from api import models
    from scripts import compact_session_replays as compactor

    assert (
        compactor._repo_partial_message_signature
        is models._partial_message_signature
    )

    sidecar = tmp_path / "partial-replays.json"
    logical_partial = {
        "role": "assistant",
        "content": "partial answer",
        "reasoning": "same reasoning",
        "_partial": True,
        "_partial_tool_calls": [
            {
                "name": "lookup",
                "args": {"query": "same"},
                "done": False,
            }
        ],
    }
    first = {
        **logical_partial,
        "timestamp": 1000,
        "model": "provider-a/model",
        "request_id": "request-a",
    }
    second = {
        **logical_partial,
        "timestamp": 2000,
        "model": "provider-b/model",
        "request_id": "request-b",
    }
    sidecar.write_text(
        json.dumps(
            {
                "session_id": "partial-replays",
                "messages": [first, second],
                "context_messages": [],
            }
        ),
        encoding="utf-8",
    )

    result = compactor.compact_sidecar(sidecar)
    repaired = json.loads(sidecar.read_text(encoding="utf-8"))

    assert result["arrays"]["messages"] == {
        "input": 2,
        "output": 1,
        "removed": 1,
    }
    assert repaired["messages"] == [first]


def test_offline_compactor_memory_is_bounded_for_many_replays(tmp_path):
    sidecar = tmp_path / "many-replays.json"
    replay = json.dumps(_row(), ensure_ascii=False, separators=(",", ":"))
    copies = 60_000
    with sidecar.open("w", encoding="utf-8") as handle:
        handle.write('{"session_id":"many-replays","message_count":')
        handle.write(str(copies))
        handle.write(',"messages":[')
        for index in range(copies):
            if index:
                handle.write(',')
            handle.write(replay)
        handle.write('],"context_messages":[]}')

    script = Path(__file__).resolve().parents[1] / "scripts" / "compact_session_replays.py"
    completed = _run_with_isolated_rss(
        script,
        sidecar,
        timeout=120,
    )

    assert completed.returncode == 0, completed.stderr or completed.stdout
    result = json.loads(completed.stdout)
    assert result["status"] == "compacted"
    assert result["arrays"]["messages"] == {
        "input": copies,
        "output": 1,
        "removed": copies - 1,
    }
    assert result["max_rss_kib"] < 256 * 1024
    repaired = json.loads(sidecar.read_text(encoding="utf-8"))
    assert repaired["_sidecar_generation_v1"] == 1
    assert repaired["message_count"] == 1
    assert repaired["messages"] == [_row()]


def test_offline_compactor_memory_is_bounded_for_many_unique_ids(tmp_path):
    sidecar = tmp_path / "many-unique-replays.json"
    copies = 500_000
    with sidecar.open("w", encoding="utf-8") as handle:
        handle.write('{"session_id":"many-unique-replays","messages":[')
        for index in range(copies):
            if index:
                handle.write(',')
            handle.write(
                json.dumps(
                    {
                        "role": "assistant",
                        "content": "",
                        "id": f"assistant-{index}",
                        "finish_reason": "stop",
                    },
                    separators=(",", ":"),
                )
            )
        handle.write('],"context_messages":[]}')

    script = Path(__file__).resolve().parents[1] / "scripts" / "compact_session_replays.py"
    completed = _run_with_isolated_rss(
        script,
        "--dry-run",
        sidecar,
        timeout=180,
    )

    assert completed.returncode == 0, completed.stderr or completed.stdout
    result = json.loads(completed.stdout)
    assert result["status"] == "no_change"
    assert result["arrays"]["messages"]["output"] == copies
    assert result["max_rss_kib"] < 192 * 1024


def test_offline_compactor_refuses_publish_after_source_drift(tmp_path, monkeypatch):
    from scripts import compact_session_replays as compactor

    sidecar = tmp_path / "session.json"
    payload = {
        "session_id": "session",
        "messages": [_row(), _row()],
        "context_messages": [],
    }
    sidecar.write_text(json.dumps(payload), encoding="utf-8")
    original_sha = _sha256(sidecar)
    backup = sidecar.with_name(
        f"{sidecar.name}.replay-v10.{original_sha[:16]}.bak"
    )
    manifest = backup.with_suffix(f"{backup.suffix}.manifest.json")
    original_hash = compactor._sha256

    def _mutate_after_output_hash(path):
        digest = original_hash(path)
        if path.name.startswith(f".{sidecar.name}.replay-v10.tmp"):
            sidecar.write_text(
                json.dumps({"session_id": "newer-writer", "messages": []}),
                encoding="utf-8",
            )
        return digest

    monkeypatch.setattr(compactor, "_sha256", _mutate_after_output_hash)

    with pytest.raises(compactor.StreamJSONError, match="source generation changed"):
        compactor.compact_sidecar(sidecar)

    assert json.loads(sidecar.read_text(encoding="utf-8"))["session_id"] == "newer-writer"
    assert _sha256(backup) == original_sha
    assert not manifest.exists()
    assert not list(tmp_path.glob(f".{sidecar.name}.replay-v10.tmp.*"))


def test_restore_refuses_sidecar_changed_after_compaction(tmp_path):
    from scripts.compact_session_replays import (
        StreamJSONError,
        compact_sidecar,
        restore_manifest,
    )

    sidecar = tmp_path / "session.json"
    sidecar.write_text(
        json.dumps({
            "session_id": "session",
            "messages": [_row(), _row()],
            "context_messages": [],
        }),
        encoding="utf-8",
    )
    result = compact_sidecar(sidecar)
    newer = json.loads(sidecar.read_text(encoding="utf-8"))
    newer["title"] = "newer writer"
    sidecar.write_text(json.dumps(newer), encoding="utf-8")
    newer_sha = _sha256(sidecar)

    with pytest.raises(StreamJSONError, match="no longer matches compacted output"):
        restore_manifest(Path(result["manifest"]))

    assert _sha256(sidecar) == newer_sha


@pytest.mark.skipif(os.name == "nt", reason="offline compactor is POSIX/WSL-only")
def test_restore_rejects_symlinked_backup(tmp_path):
    from scripts.compact_session_replays import (
        StreamJSONError,
        compact_sidecar,
        restore_manifest,
    )

    sidecar = tmp_path / "symlink-restore.json"
    sidecar.write_text(
        json.dumps({
            "session_id": "symlink-restore",
            "title": "original",
            "messages": [_row(), _row()],
            "context_messages": [],
        }),
        encoding="utf-8",
    )
    result = compact_sidecar(sidecar)
    backup = Path(result["backup"])
    copied_backup = tmp_path / "copied-backup.json"
    copied_backup.write_bytes(backup.read_bytes())
    backup.unlink()
    backup.symlink_to(copied_backup)

    with pytest.raises(StreamJSONError, match="backup.*regular file|symlink"):
        restore_manifest(Path(result["manifest"]))

    assert json.loads(sidecar.read_text(encoding="utf-8"))["messages"] == [_row()]


@pytest.mark.skipif(os.name == "nt", reason="offline compactor is POSIX/WSL-only")
def test_restore_uses_the_same_backup_descriptor_for_hash_and_copy(
    tmp_path,
    monkeypatch,
):
    from scripts import compact_session_replays as compactor

    sidecar = tmp_path / "descriptor-restore.json"
    sidecar.write_text(
        json.dumps({
            "session_id": "descriptor-restore",
            "title": "original",
            "messages": [_row(), _row()],
            "context_messages": [],
        }),
        encoding="utf-8",
    )
    result = compactor.compact_sidecar(sidecar)
    backup = Path(result["backup"])
    displaced_backup = tmp_path / "displaced-backup.json"
    real_open = compactor.os.open
    injected = []

    def interleave_backup_replacement(path, flags, mode=0o777, *, dir_fd=None):
        kwargs = {} if dir_fd is None else {"dir_fd": dir_fd}
        fd = real_open(path, flags, mode, **kwargs)
        if (
            Path(path) == backup
            and flags & os.O_ACCMODE == os.O_RDONLY
            and not injected
        ):
            backup.rename(displaced_backup)
            backup.write_text(
                json.dumps({
                    "session_id": "descriptor-restore",
                    "title": "injected replacement",
                    "messages": [],
                    "context_messages": [],
                }),
                encoding="utf-8",
            )
            injected.append(True)
        return fd

    monkeypatch.setattr(compactor.os, "open", interleave_backup_replacement)

    restored = compactor.restore_manifest(Path(result["manifest"]))

    assert injected == [True]
    assert restored["status"] == "restored"
    payload = json.loads(sidecar.read_text(encoding="utf-8"))
    assert payload["title"] == "original"
    assert payload["messages"] == [_row(), _row()]


def test_ambiguous_empty_rows_are_not_removed(tmp_path):
    from scripts.compact_session_replays import compact_sidecar

    sidecar = tmp_path / "ambiguous.json"
    bool_id = {
        "role": "assistant",
        "content": "",
        "id": True,
        "finish_reason": "stop",
    }
    structured = {
        "role": "assistant",
        "content": "",
        "id": "structured",
        "attachments": [],
    }
    sidecar.write_text(
        json.dumps({
            "session_id": "ambiguous",
            "messages": [bool_id, bool_id, structured, structured],
            "context_messages": [],
        }),
        encoding="utf-8",
    )

    result = compact_sidecar(sidecar, dry_run=True)

    assert result["status"] == "no_change"
    assert result["arrays"]["messages"]["removed"] == 0


def test_offline_compactor_serializes_runtime_writer_through_publish(
    tmp_path,
    monkeypatch,
):
    from api import models
    from scripts import compact_session_replays as compactor

    session_dir = tmp_path / "sessions"
    session_dir.mkdir()
    monkeypatch.setattr(models, "SESSION_DIR", session_dir)
    monkeypatch.setattr(models, "SESSION_INDEX_FILE", session_dir / "_index.json")
    sidecar = session_dir / "race.json"
    sidecar.write_text(
        json.dumps(
            {
                "session_id": "race",
                "title": "before",
                "messages": [_row(), _row()],
                "context_messages": [],
                "_sidecar_generation_v1": 3,
            }
        ),
        encoding="utf-8",
    )
    writer = models.Session.load("race")
    assert writer is not None
    writer.title = "newer writer"
    writer_done = threading.Event()
    writer_errors = []
    writer_thread = None
    writer_finished_before_publish = []
    real_replace = os.replace

    def _save_newer():
        try:
            writer.save(skip_index=True)
        except RuntimeError as exc:
            writer_errors.append(str(exc))
        finally:
            writer_done.set()

    def _interleave_writer(source, destination):
        nonlocal writer_thread
        source_path = Path(source)
        destination_path = Path(destination)
        if (
            destination_path == sidecar
            and ".replay-v10.tmp." in source_path.name
        ):
            writer_thread = threading.Thread(target=_save_newer)
            writer_thread.start()
            writer_finished_before_publish.append(writer_done.wait(0.25))
        return real_replace(source, destination)

    monkeypatch.setattr(compactor.os, "replace", _interleave_writer)

    compactor.compact_sidecar(sidecar)
    assert writer_thread is not None
    writer_thread.join(timeout=3)

    assert writer_finished_before_publish == [False]
    assert writer_done.is_set()
    assert writer_errors == [
        "stale sidecar revision for session 'race'; reload before saving"
    ]
    persisted = json.loads(sidecar.read_text(encoding="utf-8"))
    assert persisted["title"] == "before"
    assert persisted["messages"] == [_row()]
    assert persisted["_sidecar_generation_v1"] == 4


def test_runtime_save_preserves_and_advances_sidecar_generation(tmp_path, monkeypatch):
    from api import models

    monkeypatch.setattr(models, "SESSION_DIR", tmp_path)
    monkeypatch.setattr(models, "SESSION_INDEX_FILE", tmp_path / "_index.json")
    sidecar = tmp_path / "generation.json"
    sidecar.write_text(
        json.dumps(
            {
                "session_id": "generation",
                "messages": [{"role": "user", "content": "keep"}],
                "_sidecar_generation_v1": 7,
            }
        ),
        encoding="utf-8",
    )

    loaded = models.Session.load("generation")
    assert loaded is not None
    loaded.title = "saved"
    loaded.save(skip_index=True)

    persisted = json.loads(sidecar.read_text(encoding="utf-8"))
    assert persisted["_sidecar_generation_v1"] == 8


def test_fresh_runtime_writer_adopts_existing_sidecar_revision(tmp_path, monkeypatch):
    from api import models

    monkeypatch.setattr(models, "SESSION_DIR", tmp_path)
    monkeypatch.setattr(models, "SESSION_INDEX_FILE", tmp_path / "_index.json")
    original = models.Session(session_id="fresh-writer")
    original.messages = [{"role": "user", "content": "original"}]
    original.save(skip_index=True)
    original_payload = json.loads(original.path.read_text(encoding="utf-8"))

    replacement = models.Session(
        session_id="fresh-writer",
        title="replacement",
        messages=[{"role": "user", "content": "replacement"}],
    )
    assert replacement._sidecar_revision_v1 is None

    replacement.save(skip_index=True)

    persisted = json.loads(replacement.path.read_text(encoding="utf-8"))
    assert persisted["title"] == "replacement"
    assert persisted["messages"] == replacement.messages
    assert persisted["_sidecar_generation_v1"] == 2
    assert persisted["_sidecar_epoch_v1"] == original_payload["_sidecar_epoch_v1"]


def test_sidecar_revision_is_scoped_to_the_observed_session_id(tmp_path, monkeypatch):
    from api import models

    monkeypatch.setattr(models, "SESSION_DIR", tmp_path)
    monkeypatch.setattr(models, "SESSION_INDEX_FILE", tmp_path / "_index.json")
    session = models.Session(session_id="rotation-source")
    session.messages = [{"role": "user", "content": "keep"}]
    session.save(skip_index=True)
    source_epoch = json.loads(session.path.read_text(encoding="utf-8"))[
        "_sidecar_epoch_v1"
    ]

    session.session_id = "rotation-continuation"
    session.save(skip_index=True)

    continuation = json.loads(session.path.read_text(encoding="utf-8"))
    assert continuation["session_id"] == "rotation-continuation"
    assert continuation["messages"] == session.messages
    assert continuation["_sidecar_generation_v1"] == 1
    assert continuation["_sidecar_epoch_v1"] != source_epoch


def test_runtime_save_rejects_owner_loaded_before_offline_compaction(
    tmp_path,
    monkeypatch,
):
    from api import models
    from scripts.compact_session_replays import compact_sidecar

    monkeypatch.setattr(models, "SESSION_DIR", tmp_path)
    monkeypatch.setattr(models, "SESSION_INDEX_FILE", tmp_path / "_index.json")
    sidecar = tmp_path / "stale-owner.json"
    duplicate = _row()
    sidecar.write_text(
        json.dumps(
            {
                "session_id": "stale-owner",
                "messages": [duplicate, duplicate],
                "context_messages": [],
                "_sidecar_generation_v1": 3,
            }
        ),
        encoding="utf-8",
    )
    stale_owner = models.Session.load("stale-owner")
    assert stale_owner is not None

    compact_sidecar(sidecar)
    fresh_owner = models.Session.load("stale-owner")
    assert fresh_owner is not None
    new_turn = {"role": "user", "content": "new durable turn"}
    fresh_owner.messages.append(new_turn)
    fresh_owner.save(skip_index=True)

    stale_owner.title = "must not overwrite"
    with pytest.raises(RuntimeError, match="stale sidecar revision"):
        stale_owner.save(skip_index=True)

    persisted = json.loads(sidecar.read_text(encoding="utf-8"))
    assert persisted["messages"] == [duplicate, new_turn]
    assert persisted["_sidecar_generation_v1"] == 5


def test_runtime_save_rejects_delete_recreate_epoch_aba(tmp_path, monkeypatch):
    from api import models

    monkeypatch.setattr(models, "SESSION_DIR", tmp_path)
    monkeypatch.setattr(models, "SESSION_INDEX_FILE", tmp_path / "_index.json")
    original = models.Session(session_id="epoch-aba")
    original.messages = [{"role": "user", "content": "original"}]
    original.save(skip_index=True)
    stale_owner = models.Session.load("epoch-aba")
    assert stale_owner is not None
    original_epoch = json.loads(original.path.read_text(encoding="utf-8"))[
        "_sidecar_epoch_v1"
    ]

    original.path.unlink()
    recreated = models.Session(session_id="epoch-aba")
    recreated.messages = [{"role": "user", "content": "recreated"}]
    recreated.save(skip_index=True)
    recreated_payload = json.loads(recreated.path.read_text(encoding="utf-8"))
    assert recreated_payload["_sidecar_epoch_v1"] != original_epoch

    stale_owner.title = "must not cross epoch"
    with pytest.raises(RuntimeError, match="stale sidecar revision"):
        stale_owner.save(skip_index=True)

    assert json.loads(recreated.path.read_text(encoding="utf-8")) == recreated_payload


def test_non_target_string_is_copied_in_bounded_chunks():
    from scripts.compact_session_replays import StreamReader

    value = "x" * 200_000
    writes = []

    class RecordingWriter(io.StringIO):
        def write(self, text):
            writes.append(len(text))
            return super().write(text)

    output = RecordingWriter()
    StreamReader(io.StringIO(json.dumps(value))).copy_raw_value(output)

    assert output.getvalue() == json.dumps(value)
    assert max(writes) <= 65_536


def test_non_target_32_mib_string_stays_under_bounded_rss(tmp_path):
    sidecar = tmp_path / "large-non-target.json"
    script = (
        Path(__file__).resolve().parents[1]
        / "scripts"
        / "compact_session_replays.py"
    )
    chunk = "x" * (1 << 20)
    with sidecar.open("w", encoding="utf-8") as handle:
        handle.write('{"session_id":"large-non-target","metadata":"')
        for _ in range(32):
            handle.write(chunk)
        handle.write(
            '","messages":['
            + json.dumps(_row(), separators=(",", ":"))
            + ','
            + json.dumps(_row(), separators=(",", ":"))
            + '],"context_messages":[]}'
        )

    completed = _run_with_isolated_rss(
        script,
        sidecar,
        "--dry-run",
        timeout=120,
    )

    assert completed.returncode == 0, completed.stderr or completed.stdout
    result = json.loads(completed.stdout)
    assert result["status"] == "would_compact"
    assert result["max_rss_kib"] < 192 * 1024


def test_target_scalar_split_at_reader_boundary_is_not_truncated():
    from scripts.compact_session_replays import StreamReader

    reader = StreamReader(io.StringIO("12345]"), chunk_chars=1)

    assert reader.decode_value() == 12345
    assert reader.take() == "]"


def test_bounded_metadata_rejects_oversized_non_target_scalar(tmp_path):
    from api import models

    sidecar = tmp_path / "oversized-scalar.json"
    sidecar.write_text(
        '{"session_id":"oversized-scalar","metadata":0.'
        + ("0" * 70_000)
        + '1,"messages":[]}',
        encoding="utf-8",
    )

    with pytest.raises(ValueError, match="scalar exceeds bounded limit"):
        models._read_bounded_session_metadata(sidecar)


def test_runtime_save_tracks_actual_published_bytes(tmp_path, monkeypatch):
    from api import models

    monkeypatch.setattr(models, "SESSION_DIR", tmp_path)
    monkeypatch.setattr(models, "SESSION_INDEX_FILE", tmp_path / "_index.json")
    sidecar = tmp_path / "translated-newlines.json"
    real_replace = models._safe_replace

    def replace_with_translated_newlines(source, destination):
        real_replace(source, destination)
        if Path(destination) == sidecar:
            sidecar.write_bytes(sidecar.read_bytes().replace(b"\n", b"\r\n"))

    monkeypatch.setattr(models, "_safe_replace", replace_with_translated_newlines)
    session = models.Session(session_id="translated-newlines")
    session.messages = [{"role": "user", "content": "keep"}]
    session.save(skip_index=True)
    session.title = "second save"

    session.save(skip_index=True)

    assert json.loads(sidecar.read_text(encoding="utf-8"))["title"] == "second save"


def test_large_valid_session_can_save_after_no_change_analysis(tmp_path, monkeypatch):
    from api import models
    from scripts.compact_session_replays import compact_sidecar

    monkeypatch.setattr(models, "SESSION_DIR", tmp_path)
    monkeypatch.setattr(models, "SESSION_INDEX_FILE", tmp_path / "_index.json")
    monkeypatch.setattr(models, "_INLINE_REPLAY_REPAIR_MAX_BYTES", 1)
    sidecar = tmp_path / "large-valid.json"
    sidecar.write_text(
        json.dumps(
            {
                "session_id": "large-valid",
                "messages": [{"role": "user", "content": "unique"}],
                "context_messages": [],
            }
        ),
        encoding="utf-8",
    )

    assert compact_sidecar(sidecar, dry_run=True)["status"] == "no_change"
    loaded = models.Session.load("large-valid")
    assert loaded is not None
    assert loaded._replay_repair_deferred is False
    loaded.title = "still writable"
    loaded.save(skip_index=True)
    assert json.loads(sidecar.read_text(encoding="utf-8"))["title"] == "still writable"


def test_compactor_preserves_private_source_mode(tmp_path):
    from scripts.compact_session_replays import compact_sidecar, restore_manifest

    sidecar = tmp_path / "private.json"
    sidecar.write_text(
        json.dumps(
            {
                "session_id": "private",
                "messages": [_row(), _row()],
                "context_messages": [],
            }
        ),
        encoding="utf-8",
    )
    sidecar.chmod(0o600)

    result = compact_sidecar(sidecar)
    backup = Path(result["backup"])
    manifest = Path(result["manifest"])
    assert stat.S_IMODE(sidecar.stat().st_mode) == 0o600
    assert stat.S_IMODE(backup.stat().st_mode) == 0o600
    assert stat.S_IMODE(manifest.stat().st_mode) == 0o600

    restore_manifest(manifest)
    assert stat.S_IMODE(sidecar.stat().st_mode) == 0o600


@pytest.mark.skipif(os.name == "nt", reason="offline compactor is POSIX/WSL-only")
def test_sensitive_artifacts_are_created_private_before_fchmod(
    tmp_path,
    monkeypatch,
):
    from scripts import compact_session_replays as compactor

    sidecar = tmp_path / "private-at-create.json"
    sidecar.write_text(
        json.dumps({
            "session_id": "private-at-create",
            "messages": [_row(), _row()],
            "context_messages": [],
        }),
        encoding="utf-8",
    )
    sidecar.chmod(0o600)
    modes_before_fchmod = []
    real_fchmod = compactor.os.fchmod

    def record_initial_mode(fd, mode):
        modes_before_fchmod.append(stat.S_IMODE(os.fstat(fd).st_mode))
        return real_fchmod(fd, mode)

    monkeypatch.setattr(compactor.os, "fchmod", record_initial_mode)
    previous_umask = os.umask(0)
    try:
        result = compactor.compact_sidecar(sidecar)
        compactor.restore_manifest(Path(result["manifest"]))
    finally:
        os.umask(previous_umask)

    assert len(modes_before_fchmod) >= 4
    assert set(modes_before_fchmod) == {0o600}


def test_compactor_rejects_invalid_non_target_json_without_publish(tmp_path):
    from scripts import compact_session_replays as compactor

    sidecar = tmp_path / "invalid.json"
    original = (
        '{"session_id":"invalid","metadata":{"broken":},'
        '"messages":['
        + json.dumps(_row(), separators=(",", ":"))
        + ','
        + json.dumps(_row(), separators=(",", ":"))
        + '],"context_messages":[]}'
    )
    sidecar.write_text(original, encoding="utf-8")

    with pytest.raises(compactor.StreamJSONError, match="invalid JSON"):
        compactor.compact_sidecar(sidecar)

    assert sidecar.read_text(encoding="utf-8") == original
    assert not list(tmp_path.glob("*manifest*.json"))


def test_compactor_rejects_nonstandard_target_constant_without_publish(tmp_path):
    from scripts import compact_session_replays as compactor

    sidecar = tmp_path / "nonstandard.json"
    original = (
        '{"session_id":"nonstandard","messages":[NaN],'
        '"context_messages":[]}'
    )
    sidecar.write_text(original, encoding="utf-8")

    with pytest.raises(compactor.StreamJSONError, match="invalid JSON constant"):
        compactor.compact_sidecar(sidecar)

    assert sidecar.read_text(encoding="utf-8") == original
    assert not list(tmp_path.glob("*manifest*.json"))


def test_compactor_manifest_is_excluded_from_session_index(tmp_path, monkeypatch):
    from api import models
    from scripts.compact_session_replays import compact_sidecar

    monkeypatch.setattr(models, "SESSION_DIR", tmp_path)
    monkeypatch.setattr(models, "SESSION_INDEX_FILE", tmp_path / "_index.json")
    monkeypatch.setattr(models, "SESSIONS", type(models.SESSIONS)())
    sidecar = tmp_path / "indexed.json"
    sidecar.write_text(
        json.dumps(
            {
                "session_id": "indexed",
                "messages": [_row(), _row()],
                "context_messages": [],
            }
        ),
        encoding="utf-8",
    )

    result = compact_sidecar(sidecar)
    assert Path(result["manifest"]).name.startswith("_")
    models._write_session_index(updates=None)
    index = json.loads((tmp_path / "_index.json").read_text(encoding="utf-8"))
    assert [row["session_id"] for row in index] == ["indexed"]


def test_session_delete_cleanup_removes_all_replay_v10_plaintext_artifacts(tmp_path):
    from api import models

    sidecar = tmp_path / "privacy-delete.json"
    artifacts = [
        tmp_path / "privacy-delete.json.replay-v10.abcdef0123456789.bak",
        tmp_path / "_replay-v10.privacy-delete.json.abcdef0123456789.manifest.json",
        tmp_path / ".privacy-delete.json.replay-v10.tmp.123",
        tmp_path / ".privacy-delete.json.replay-v10.restore.123",
        tmp_path / "._replay-v10.privacy-delete.json.abcdef0123456789.manifest.json.tmp.123",
    ]
    unrelated = tmp_path / "other.json.replay-v10.abcdef0123456789.bak"
    for artifact in [*artifacts, unrelated]:
        artifact.write_text("plaintext transcript", encoding="utf-8")

    removed = models._delete_offline_replay_artifacts(sidecar)

    assert removed == len(artifacts)
    assert not any(artifact.exists() for artifact in artifacts)
    assert unrelated.exists()


def test_webui_session_delete_routes_replay_v10_cleanup():
    routes_source = (
        Path(__file__).resolve().parents[1] / "api" / "routes.py"
    ).read_text(encoding="utf-8")
    start = routes_source.index('parsed.path == "/api/session/delete"')
    end = routes_source.index('parsed.path == "/api/session/clear"', start)

    assert "_delete_offline_replay_artifacts(p)" in routes_source[start:end]


def test_large_index_rebuild_uses_metadata_prefix(tmp_path, monkeypatch):
    from api import models

    monkeypatch.setattr(models, "SESSION_DIR", tmp_path)
    monkeypatch.setattr(models, "SESSION_INDEX_FILE", tmp_path / "_index.json")
    monkeypatch.setattr(models, "SESSIONS", type(models.SESSIONS)())
    monkeypatch.setattr(models, "_INLINE_REPLAY_REPAIR_MAX_BYTES", 1)
    sidecar = tmp_path / "large-index.json"
    sidecar.write_text(
        json.dumps(
            {
                "session_id": "large-index",
                "title": "Large",
                "created_at": 1,
                "updated_at": 2,
                "message_count": 2,
                "messages": [
                    {"role": "user", "content": "one"},
                    {"role": "assistant", "content": "two"},
                ],
            }
        ),
        encoding="utf-8",
    )

    def _must_not_collapse(_messages):
        raise AssertionError("large index rebuild full-parsed the transcript")

    monkeypatch.setattr(models, "_collapse_adjacent_duplicate_partials", _must_not_collapse)
    models._write_session_index(updates=None)

    index = json.loads((tmp_path / "_index.json").read_text(encoding="utf-8"))
    assert len(index) == 1
    assert index[0]["session_id"] == "large-index"
    assert index[0]["message_count"] == 2


def test_large_index_rebuild_scans_metadata_beyond_prefix_and_arrays(
    tmp_path,
    monkeypatch,
):
    from api import models

    monkeypatch.setattr(models, "SESSION_DIR", tmp_path)
    monkeypatch.setattr(models, "SESSION_INDEX_FILE", tmp_path / "_index.json")
    monkeypatch.setattr(models, "SESSIONS", type(models.SESSIONS)())
    monkeypatch.setattr(models, "_INLINE_REPLAY_REPAIR_MAX_BYTES", 1)

    oversized = tmp_path / "oversized-prefix.json"
    oversized.write_text(
        json.dumps(
            {
                "session_id": "oversized-prefix",
                "compression_anchor_summary": "x" * 70_000,
                "title": "Oversized prefix",
                "messages": [{"role": "user", "content": "keep"}],
                "message_count": 1,
                "updated_at": 2,
            }
        ),
        encoding="utf-8",
    )
    messages_first = tmp_path / "messages-first.json"
    messages_first.write_text(
        json.dumps(
            {
                "messages": [{"role": "user", "content": "keep"}],
                "session_id": "messages-first",
                "title": "Messages first",
                "updated_at": 3,
            }
        ),
        encoding="utf-8",
    )
    legacy_scenes_first = tmp_path / "legacy-scenes-first.json"
    legacy_scenes_first.write_text(
        json.dumps(
            {
                "session_id": "legacy-scenes-first",
                "title": "Legacy scenes first",
                "anchor_activity_scenes": {
                    "scene": {"rows": ["x" * 1024 for _ in range(80)]}
                },
                "message_count": 2,
                "messages": [
                    {"role": "user", "content": "one"},
                    {"role": "assistant", "content": "two"},
                ],
                "updated_at": 4,
            }
        ),
        encoding="utf-8",
    )

    models._write_session_index(updates=None)

    index = json.loads((tmp_path / "_index.json").read_text(encoding="utf-8"))
    by_id = {row["session_id"]: row for row in index}
    assert set(by_id) == {
        "oversized-prefix",
        "messages-first",
        "legacy-scenes-first",
    }
    assert by_id["oversized-prefix"]["message_count"] == 1
    assert by_id["messages-first"]["message_count"] == 1
    assert by_id["legacy-scenes-first"]["message_count"] == 2
    assert by_id["messages-first"]["title"] == "Messages first"


def test_large_index_rebuild_rejects_malformed_and_foreign_session_ids(
    tmp_path,
    monkeypatch,
):
    from api import models

    monkeypatch.setattr(models, "SESSION_DIR", tmp_path)
    monkeypatch.setattr(models, "SESSION_INDEX_FILE", tmp_path / "_index.json")
    monkeypatch.setattr(models, "SESSIONS", type(models.SESSIONS)())
    monkeypatch.setattr(models, "_INLINE_REPLAY_REPAIR_MAX_BYTES", 1)
    malformed = tmp_path / "malformed.json"
    malformed.write_text(
        '{"session_id":"malformed","junk":not_json,"messages":[]}',
        encoding="utf-8",
    )
    foreign = tmp_path / "filename-authority.json"
    foreign.write_text(
        json.dumps(
            {
                "session_id": "foreign-id",
                "title": "must not be indexed",
                "messages": [],
            }
        ),
        encoding="utf-8",
    )

    models._write_session_index(updates=None)

    index = json.loads((tmp_path / "_index.json").read_text(encoding="utf-8"))
    assert index == []
    assert models.Session.load("foreign-id") is None


def test_large_index_rebuild_uses_last_duplicate_messages_array(
    tmp_path,
    monkeypatch,
):
    from api import models

    monkeypatch.setattr(models, "SESSION_DIR", tmp_path)
    monkeypatch.setattr(models, "SESSION_INDEX_FILE", tmp_path / "_index.json")
    monkeypatch.setattr(models, "SESSIONS", type(models.SESSIONS)())
    monkeypatch.setattr(models, "_INLINE_REPLAY_REPAIR_MAX_BYTES", 1)
    sidecar = tmp_path / "duplicate-messages.json"
    sidecar.write_text(
        '{"session_id":"duplicate-messages",'
        '"messages":[{"role":"user"},{"role":"assistant"}],'
        '"messages":[{"role":"user"}]}',
        encoding="utf-8",
    )

    models._write_session_index(updates=None)

    index = json.loads((tmp_path / "_index.json").read_text(encoding="utf-8"))
    assert len(index) == 1
    assert index[0]["session_id"] == "duplicate-messages"
    assert index[0]["message_count"] == 1


def test_actual_over_128_mib_index_load_streams_messages_first(tmp_path):
    from api import models

    sidecar = tmp_path / "actual-large.json"
    payload_bytes = models._INLINE_REPLAY_REPAIR_MAX_BYTES + 1
    chunk = "x" * (1 << 20)
    with sidecar.open("w", encoding="utf-8") as handle:
        handle.write('{"messages":[{"role":"user","content":"')
        remaining = payload_bytes
        while remaining:
            part = chunk[: min(len(chunk), remaining)]
            handle.write(part)
            remaining -= len(part)
        handle.write(
            '"}],"session_id":"actual-large","title":"Actual large",'
            '"updated_at":5}'
        )

    loaded = models._load_session_from_path(sidecar)

    assert loaded is not None
    assert loaded.session_id == "actual-large"
    assert loaded.title == "Actual large"
    assert loaded._metadata_message_count == 1


@pytest.mark.parametrize("bad_ws", ["\v", "\f"])
@pytest.mark.parametrize(
    "location",
    ["top-level", "nested", "target-array", "trailing"],
)
def test_compactor_rejects_non_json_whitespace_without_publish(
    tmp_path,
    bad_ws,
    location,
):
    from scripts import compact_session_replays as compactor

    row = json.dumps(_row(), separators=(",", ":"))
    if location == "top-level":
        original = (
            '{"session_id":"invalid"'
            + bad_ws
            + ',"messages":['
            + row
            + ','
            + row
            + '],"context_messages":[]}'
        )
    elif location == "nested":
        original = (
            '{"session_id":"invalid","metadata":{"value":1'
            + bad_ws
            + '},"messages":['
            + row
            + ','
            + row
            + '],"context_messages":[]}'
        )
    elif location == "target-array":
        original = (
            '{"session_id":"invalid","messages":['
            + row
            + bad_ws
            + ','
            + row
            + '],"context_messages":[]}'
        )
    else:
        original = (
            '{"session_id":"invalid","messages":['
            + row
            + ','
            + row
            + '],"context_messages":[]}'
            + bad_ws
        )
    sidecar = tmp_path / f"invalid-{location}.json"
    sidecar.write_text(original, encoding="utf-8")

    with pytest.raises(compactor.StreamJSONError):
        compactor.compact_sidecar(sidecar)

    assert sidecar.read_text(encoding="utf-8") == original
    assert not list(tmp_path.glob("*.bak"))
    assert not list(tmp_path.glob("*manifest*.json"))


def test_compactor_fsyncs_manifest_name_before_source_install(tmp_path, monkeypatch):
    from scripts import compact_session_replays as compactor

    sidecar = tmp_path / "ordered.json"
    sidecar.write_text(
        json.dumps(
            {
                "session_id": "ordered",
                "messages": [_row(), _row()],
                "context_messages": [],
            }
        ),
        encoding="utf-8",
    )
    events = []
    real_replace = compactor.os.replace

    def _record_replace(source, destination):
        destination = Path(destination)
        events.append(("replace", "source" if destination == sidecar else "manifest"))
        return real_replace(source, destination)

    def _record_fsync_dir(path):
        events.append(("fsync_dir", Path(path)))

    monkeypatch.setattr(compactor.os, "replace", _record_replace)
    monkeypatch.setattr(compactor, "_fsync_dir", _record_fsync_dir)

    compactor.compact_sidecar(sidecar)

    manifest_replace = events.index(("replace", "manifest"))
    source_replace = events.index(("replace", "source"))
    assert any(
        event[0] == "fsync_dir"
        for event in events[manifest_replace + 1 : source_replace]
    )


def test_compactor_removes_published_manifest_when_source_install_fails(
    tmp_path,
    monkeypatch,
):
    from scripts import compact_session_replays as compactor

    sidecar = tmp_path / "failed-install.json"
    original = json.dumps(
        {
            "session_id": "failed-install",
            "messages": [_row(), _row()],
            "context_messages": [],
        }
    )
    sidecar.write_text(original, encoding="utf-8")
    real_replace = compactor.os.replace

    def _fail_source_install(source, destination):
        if Path(destination) == sidecar:
            raise OSError("injected source install failure")
        return real_replace(source, destination)

    monkeypatch.setattr(compactor.os, "replace", _fail_source_install)

    with pytest.raises(OSError, match="injected source install failure"):
        compactor.compact_sidecar(sidecar)

    assert sidecar.read_text(encoding="utf-8") == original
    assert not list(tmp_path.glob("*manifest*.json"))
    assert not list(tmp_path.glob(".*.tmp.*"))


def test_cli_rejects_dry_run_restore(tmp_path, monkeypatch):
    from scripts import compact_session_replays as compactor

    manifest = tmp_path / "manifest.json"
    manifest.write_text("{}", encoding="utf-8")
    monkeypatch.setattr(
        sys,
        "argv",
        ["compact_session_replays.py", "--dry-run", "--restore", str(manifest)],
    )
    monkeypatch.setattr(
        compactor,
        "restore_manifest",
        lambda _path: pytest.fail("--dry-run must not restore"),
    )

    with pytest.raises(SystemExit) as exc_info:
        compactor.main()

    assert exc_info.value.code == 2


def test_state_divergence_ignores_current_hidden_manifests(
    tmp_path,
    monkeypatch,
    capsys,
):
    from api import config

    current = tmp_path / "current"
    current_sessions = current / "sessions"
    current_sessions.mkdir(parents=True)
    (current_sessions / "_replay-v10.manifest.json").write_text(
        "{}",
        encoding="utf-8",
    )
    sibling = tmp_path / "sibling"
    sibling_sessions = sibling / "sessions"
    sibling_sessions.mkdir(parents=True)
    (sibling_sessions / "real-session.json").write_text("{}", encoding="utf-8")
    monkeypatch.setattr(config, "STATE_DIR", current)
    monkeypatch.setattr(config, "SESSION_DIR", current_sessions)
    monkeypatch.setattr(config, "SESSION_INDEX_FILE", current_sessions / "_index.json")

    config._warn_state_dir_divergence("WARN")

    assert str(sibling) in capsys.readouterr().out


def test_state_divergence_ignores_sibling_hidden_manifests(
    tmp_path,
    monkeypatch,
    capsys,
):
    from api import config

    current = tmp_path / "current"
    current_sessions = current / "sessions"
    current_sessions.mkdir(parents=True)
    sibling = tmp_path / "sibling"
    sibling_sessions = sibling / "sessions"
    sibling_sessions.mkdir(parents=True)
    (sibling_sessions / "_replay-v10.manifest.json").write_text(
        "{}",
        encoding="utf-8",
    )
    monkeypatch.setattr(config, "STATE_DIR", current)
    monkeypatch.setattr(config, "SESSION_DIR", current_sessions)
    monkeypatch.setattr(config, "SESSION_INDEX_FILE", current_sessions / "_index.json")

    config._warn_state_dir_divergence("WARN")

    assert capsys.readouterr().out == ""
