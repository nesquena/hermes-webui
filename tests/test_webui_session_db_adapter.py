import json
from pathlib import Path

import pytest

import api.config as config
import api.models as models
import api.webui_session_db as session_db
from api.webui_session_db import WebUIJsonSessionDB


@pytest.fixture
def session_dir(tmp_path, monkeypatch):
    path = tmp_path / "sessions"
    path.mkdir()
    monkeypatch.setattr(models, "SESSION_DIR", path)
    return path


def _write_json_session(session_dir, sid="session_1", **overrides):
    payload = {
        "session_id": sid,
        "title": "Adapter Session",
        "workspace": str(session_dir.parent),
        "model": "gpt-test",
        "model_provider": "openai",
        "created_at": 100.0,
        "updated_at": 200.0,
        "pinned": False,
        "archived": False,
        "profile": "default",
        "messages": [
            {"role": "user", "content": "hello"},
            {"role": "assistant", "content": "world"},
        ],
        "tool_calls": [{"id": "tool-1", "name": "demo"}],
    }
    payload.update(overrides)
    payload["message_count"] = len(payload["messages"])
    path = session_dir / f"{sid}.json"
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    return payload, path


def test_list_and_read_existing_json_sessions(session_dir):
    payload, _path = _write_json_session(session_dir)
    db = WebUIJsonSessionDB()

    rows = db.list_sessions()
    loaded = db.read_session(payload["session_id"])

    assert [row["session_id"] for row in rows] == [payload["session_id"]]
    assert rows[0]["title"] == payload["title"]
    assert rows[0]["message_count"] == 2
    assert loaded == payload


def test_metadata_update_survives_reload_and_preserves_messages(session_dir):
    payload, path = _write_json_session(session_dir)
    db = WebUIJsonSessionDB()

    db.update_metadata(payload["session_id"], {"title": "Renamed", "pinned": True})
    reloaded = json.loads(path.read_text(encoding="utf-8"))

    assert reloaded["title"] == "Renamed"
    assert reloaded["pinned"] is True
    assert reloaded["messages"] == payload["messages"]
    assert reloaded["tool_calls"] == payload["tool_calls"]
    assert reloaded["message_count"] == len(payload["messages"])


def test_metadata_update_rejects_unsafe_fields(session_dir):
    payload, path = _write_json_session(session_dir)
    before = path.read_text(encoding="utf-8")
    db = WebUIJsonSessionDB()

    with pytest.raises(ValueError):
        db.update_metadata(payload["session_id"], {"messages": []})
    with pytest.raises(ValueError):
        db.update_metadata(payload["session_id"], {"unknown_field": "unsafe"})

    assert path.read_text(encoding="utf-8") == before


def test_metadata_update_refuses_metadata_only_stub(session_dir):
    sid = "stub_session"
    path = session_dir / f"{sid}.json"
    path.write_text(
        json.dumps({"session_id": sid, "title": "Stub"}, indent=2),
        encoding="utf-8",
    )

    with pytest.raises(ValueError, match="metadata-only"):
        WebUIJsonSessionDB().update_metadata(sid, {"title": "Nope"})


def test_archive_unarchive_round_trip(session_dir):
    payload, path = _write_json_session(session_dir)
    db = WebUIJsonSessionDB()

    archived = db.archive(payload["session_id"])
    unarchived = db.archive(payload["session_id"], archived=False)
    reloaded = json.loads(path.read_text(encoding="utf-8"))

    assert archived["archived"] is True
    assert unarchived["archived"] is False
    assert reloaded["archived"] is False
    assert reloaded["messages"] == payload["messages"]


def test_read_only_operations_do_not_mutate_files(session_dir):
    payload, path = _write_json_session(session_dir)
    before_text = path.read_text(encoding="utf-8")
    before_stat = path.stat()
    db = WebUIJsonSessionDB()

    assert db.list_sessions()
    assert db.read_session(payload["session_id"]) == payload

    after_stat = path.stat()
    assert path.read_text(encoding="utf-8") == before_text
    assert after_stat.st_mtime_ns == before_stat.st_mtime_ns
    assert after_stat.st_size == before_stat.st_size


def test_sort_timestamp_falls_back_past_missing_values():
    assert WebUIJsonSessionDB._sort_timestamp({"last_message_at": None, "updated_at": 25.0}) == 25.0
    assert WebUIJsonSessionDB._sort_timestamp({"last_message_at": "", "created_at": "15.5"}) == 15.5


def test_module_level_write_session_wrapper(session_dir):
    payload, _path = _write_json_session(session_dir, sid="wrapper_session")
    written = session_db.write_session(payload)

    assert written == payload
    assert session_db.read_session(payload["session_id"]) == payload


def test_unified_session_db_flag_default_remains_false(monkeypatch, tmp_path):
    cfg_path = tmp_path / "missing-config.yaml"
    monkeypatch.setattr(config, "_get_config_path", lambda: cfg_path)

    config.reload_config()

    assert config.get_config()["experimental"]["unified_session_db"] is False
    assert config.is_unified_session_db_enabled() is False
    assert config.is_unified_session_db_enabled({"experimental": {"unified_session_db": True}}) is True


def test_adapter_docs_pin_runtime_wiring_preconditions():
    doc = (Path(__file__).resolve().parents[1] / "docs" / "architecture" / "unified-session-db.md").read_text(
        encoding="utf-8"
    )

    assert "Runtime Wiring Preconditions" in doc
    assert "per-session mutation locks" in doc
    assert "in-memory `Session` cache and `_index.json`" in doc
    assert "pending first turns" in doc
    assert "test/migration helpers, not runtime persistence replacements" in doc
    assert "Runtime wiring must add Session lock/cache/index parity" in (
        session_db.WebUIJsonSessionDB.update_metadata.__doc__ or ""
    )
