"""SQLite-backed session store regression tests."""
from __future__ import annotations

import json
import tempfile
from pathlib import Path

import api.models as models
import api.webui_session_sqlite as sqlite_db


def _tmp_session_dir():
    return Path(tempfile.mkdtemp())


def _sample_session_dict(sid: str = "test-session") -> dict:
    return {
        "session_id": sid,
        "title": "Test Session",
        "workspace": "/workspace",
        "model": "gpt-4",
        "created_at": 1000.0,
        "updated_at": 1001.0,
        "messages": [
            {"role": "user", "content": "hello", "timestamp": 1000.0},
            {"role": "assistant", "content": "hi", "timestamp": 1001.0},
        ],
        "tool_calls": [],
        "context_messages": [],
        "anchor_activity_scenes": {},
    }


def test_sqlite_store_round_trip():
    d = _tmp_session_dir()
    store = sqlite_db.WebUISqliteSessionDB(session_dir=d)
    payload = _sample_session_dict("sid-1")
    store.write_session(payload)

    loaded = store.read_session("sid-1")
    assert loaded is not None
    assert loaded["session_id"] == "sid-1"
    assert len(loaded["messages"]) == 2
    assert loaded["messages"][0]["role"] == "user"


def test_sqlite_store_update_metadata():
    d = _tmp_session_dir()
    store = sqlite_db.WebUISqliteSessionDB(session_dir=d)
    store.write_session(_sample_session_dict("sid-2"))

    store.update_metadata("sid-2", {"composer_draft": {"text": "draft", "files": []}})
    loaded = store.read_session("sid-2")
    assert loaded["composer_draft"]["text"] == "draft"
    assert len(loaded["messages"]) == 2


def test_session_load_uses_sqlite_when_db_exists(monkeypatch):
    d = _tmp_session_dir()
    # Patch global session dir for this test.
    monkeypatch.setattr(models, "SESSION_DIR", d)
    monkeypatch.setattr(models, "SESSION_INDEX_FILE", d / "_index.json")
    monkeypatch.setattr(models, "_sqlite_session_store_instance", None)

    store = sqlite_db.WebUISqliteSessionDB(session_dir=d)
    store.write_session(_sample_session_dict("sid-3"))

    s = models.Session.load("sid-3")
    assert s is not None
    assert s.session_id == "sid-3"
    assert len(s.messages) == 2


def test_persisted_session_ids_includes_sqlite(monkeypatch):
    d = _tmp_session_dir()
    monkeypatch.setattr(models, "SESSION_DIR", d)
    monkeypatch.setattr(models, "SESSION_INDEX_FILE", d / "_index.json")
    monkeypatch.setattr(models, "_PERSISTED_SESSION_IDS_CACHE", (None, None, frozenset()))
    monkeypatch.setattr(models, "_sqlite_session_store_instance", None)

    store = sqlite_db.WebUISqliteSessionDB(session_dir=d)
    store.write_session(_sample_session_dict("sid-4"))

    ids = models._persisted_session_ids_snapshot()
    assert "sid-4" in ids


def test_index_entry_exists_for_sqlite_only(monkeypatch):
    d = _tmp_session_dir()
    monkeypatch.setattr(models, "SESSION_DIR", d)
    monkeypatch.setattr(models, "SESSION_INDEX_FILE", d / "_index.json")
    monkeypatch.setattr(models, "_sqlite_session_store_instance", None)

    store = sqlite_db.WebUISqliteSessionDB(session_dir=d)
    store.write_session(_sample_session_dict("sid-5"))

    assert models._index_entry_exists("sid-5", in_memory_ids=set()) is True
    assert models._index_entry_exists("missing", in_memory_ids=set()) is False


def test_session_save_metadata_sqlite_only(monkeypatch):
    d = _tmp_session_dir()
    monkeypatch.setattr(models, "SESSION_DIR", d)
    monkeypatch.setattr(models, "SESSION_INDEX_FILE", d / "_index.json")
    monkeypatch.setattr(models, "_sqlite_session_store_instance", None)

    store = sqlite_db.WebUISqliteSessionDB(session_dir=d)
    store.write_session(_sample_session_dict("sid-6"))

    s = models.Session.load("sid-6")
    s.save_metadata({"composer_draft": {"text": "quick draft", "files": []}})

    reloaded = models.Session.load("sid-6")
    assert reloaded.composer_draft["text"] == "quick draft"
    assert len(reloaded.messages) == 2


def test_session_load_falls_back_to_json_when_sqlite_misses_row(monkeypatch):
    """If sessions.db exists but a session was not migrated, JSON sidecar is used."""
    d = _tmp_session_dir()
    monkeypatch.setattr(models, "SESSION_DIR", d)
    monkeypatch.setattr(models, "SESSION_INDEX_FILE", d / "_index.json")
    monkeypatch.setattr(models, "_sqlite_session_store_instance", None)

    # Create a JSON-only session.
    sid = "sid-json-only"
    payload = _sample_session_dict(sid)
    (d / f"{sid}.json").write_text(json.dumps(payload), encoding="utf-8")

    # Activating SQLite must not hide the JSON-only session.
    _ = sqlite_db.WebUISqliteSessionDB(session_dir=d)
    s = models.Session.load(sid)
    assert s is not None
    assert s.session_id == sid
    assert len(s.messages) == 2


def test_save_metadata_falls_back_to_json_for_unmigrated_session(monkeypatch):
    """sessions.db exists but the session is JSON-only: draft autosave must
    persist to the sidecar, not raise KeyError from a zero-row SQLite UPDATE."""
    d = _tmp_session_dir()
    monkeypatch.setattr(models, "SESSION_DIR", d)
    monkeypatch.setattr(models, "SESSION_INDEX_FILE", d / "_index.json")
    monkeypatch.setattr(models, "_sqlite_session_store_instance", None)

    sid = "sid-json-draft"
    payload = _sample_session_dict(sid)
    (d / f"{sid}.json").write_text(json.dumps(payload), encoding="utf-8")

    # Activate the SQLite store without migrating this session.
    _ = sqlite_db.WebUISqliteSessionDB(session_dir=d)

    s = models.Session.load(sid)
    assert s is not None
    s.save_metadata({"composer_draft": {"text": "json draft", "files": []}})

    on_disk = json.loads((d / f"{sid}.json").read_text(encoding="utf-8"))
    assert on_disk["composer_draft"]["text"] == "json draft"

    reloaded = models.Session.load(sid)
    assert reloaded.composer_draft["text"] == "json draft"
    assert len(reloaded.messages) == 2


def test_session_exists():
    d = _tmp_session_dir()
    store = sqlite_db.WebUISqliteSessionDB(session_dir=d)
    store.write_session(_sample_session_dict("sid-exists"))

    assert store.session_exists("sid-exists") is True
    assert store.session_exists("sid-missing") is False
    assert store.session_exists("../escape") is False


def test_update_metadata_does_not_advance_updated_at_for_drafts():
    d = _tmp_session_dir()
    store = sqlite_db.WebUISqliteSessionDB(session_dir=d)
    store.write_session(_sample_session_dict("sid-draft"))

    loaded = store.read_session("sid-draft")
    original_updated_at = loaded["updated_at"]

    store.update_metadata("sid-draft", {"composer_draft": {"text": "draft", "files": []}})

    reloaded = store.read_session("sid-draft")
    assert reloaded["composer_draft"]["text"] == "draft"
    assert reloaded["updated_at"] == original_updated_at


def test_update_metadata_advances_updated_at_when_requested():
    d = _tmp_session_dir()
    store = sqlite_db.WebUISqliteSessionDB(session_dir=d)
    store.write_session(_sample_session_dict("sid-touch"))

    new_time = 9999.0
    store.update_metadata("sid-touch", {"updated_at": new_time})

    reloaded = store.read_session("sid-touch")
    assert reloaded["updated_at"] == new_time
