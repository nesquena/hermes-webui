"""Opt-in migration for session files written before the save-boundary projection."""
from __future__ import annotations

import base64
import importlib.util
import json
from pathlib import Path

import pytest


def _load_script():
    path = Path(__file__).resolve().parent.parent / "scripts" / "migrate_persisted_image_payloads.py"
    spec = importlib.util.spec_from_file_location("migrate_persisted_image_payloads", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _legacy_payload(sid: str):
    raw = base64.b64encode(b"\xff\xd8\xff\xe0" + b"A" * 300_000).decode()
    part = {"type": "image_url", "image_url": {"url": "data:image/jpeg;base64," + raw}}
    return {
        "session_id": sid,
        "message_count": 2,
        "messages": [
            {"role": "user", "content": [{"type": "text", "text": "look"}, part]},
            {"role": "assistant", "content": "ok"},
        ],
        "context_messages": [{"role": "user", "content": [part]}],
    }


def test_dry_run_writes_nothing(tmp_path):
    script = _load_script()
    target = tmp_path / "legacy.json"
    target.write_text(json.dumps(_legacy_payload("legacy")))
    before = target.read_text()

    assert script.main(["--session-dir", str(tmp_path)]) == 0

    assert target.read_text() == before
    assert not (tmp_path / "legacy.json.preimage-migration.bak").exists()


def test_apply_bounds_display_transcript_and_keeps_backup(tmp_path):
    script = _load_script()
    target = tmp_path / "legacy.json"
    original = json.dumps(_legacy_payload("legacy"))
    target.write_text(original)

    assert script.main(["--session-dir", str(tmp_path), "--apply"]) == 0

    migrated_text = target.read_text()
    migrated = json.loads(migrated_text)
    # Display transcript is bounded; the provider replay prefix is preserved
    # exactly (existing round-trip contract), so it still holds the bytes.
    assert "data:image" not in json.dumps(migrated["messages"])
    assert migrated["context_messages"] == _legacy_payload("legacy")["context_messages"]
    assert len(migrated["messages"]) == 2
    assert migrated["message_count"] == 2
    assert migrated["messages"][1]["content"] == "ok"
    backup = tmp_path / "legacy.json.preimage-migration.bak"
    assert backup.exists() and backup.read_text() == original
    # Only the display copy is reclaimed here; the replay prefix is untouched.
    assert len(json.dumps(migrated["messages"])) < len(json.dumps(json.loads(original)["messages"])) // 10
    assert len(migrated_text) < len(original)


def test_include_context_opts_into_bounding_the_replay_prefix(tmp_path):
    script = _load_script()
    target = tmp_path / "legacy.json"
    target.write_text(json.dumps(_legacy_payload("legacy")))

    assert script.main(["--session-dir", str(tmp_path), "--apply", "--include-context"]) == 0

    migrated_text = target.read_text()
    assert "data:image" not in migrated_text
    migrated = json.loads(migrated_text)
    assert len(migrated["messages"]) == 2
    assert migrated["context_messages"][0]["content"][0] == {"type": "text", "text": "[image attachment]"}


def test_clean_files_are_untouched(tmp_path):
    script = _load_script()
    clean = {"session_id": "clean", "message_count": 1, "messages": [{"role": "user", "content": "hi"}]}
    target = tmp_path / "clean.json"
    text = json.dumps(clean)
    target.write_text(text)

    assert script.main(["--session-dir", str(tmp_path), "--apply"]) == 0

    assert target.read_text() == text
    assert not (tmp_path / "clean.json.preimage-migration.bak").exists()


def test_backup_is_never_overwritten(tmp_path):
    script = _load_script()
    target = tmp_path / "legacy.json"
    target.write_text(json.dumps(_legacy_payload("legacy")))
    backup = tmp_path / "legacy.json.preimage-migration.bak"
    backup.write_text("PRE-EXISTING")

    assert script.main(["--session-dir", str(tmp_path), "--apply"]) == 0

    assert backup.read_text() == "PRE-EXISTING"
