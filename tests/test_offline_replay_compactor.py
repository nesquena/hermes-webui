import hashlib
import json
import subprocess
import sys
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
    payload = {
        "session_id": sid,
        "messages": [_row(), _row()],
        "context_messages": [_row(), _row()],
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
    repaired = json.loads(sidecar.read_text(encoding="utf-8"))

    assert result["status"] == "compacted"
    assert repaired["_sidecar_generation_v1"] == 8
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

    restored = restore_manifest(manifest)
    assert restored["status"] == "restored"
    restored_payload = json.loads(sidecar.read_text(encoding="utf-8"))
    assert restored_payload.pop("_sidecar_generation_v1") == 9
    expected_payload = dict(payload)
    expected_payload.pop("_sidecar_generation_v1")
    assert restored_payload == expected_payload
    assert restored["output_sha256"] == _sha256(sidecar)
    assert _sha256(backup) == original_sha


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
        handle.write('{"session_id":"many","message_count":')
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
        handle.write('{"session_id":"many-unique","messages":[')
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
