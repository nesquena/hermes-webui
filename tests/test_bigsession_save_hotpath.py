"""Big-session save-hotpath regression tests (2026-07-13).

Covers the changes that keep a multi-MB session usable:

1. Session.save() writes compact JSON (no indent) that every existing reader —
   full json.loads and the metadata-prefix scanner — still parses.
2. The #1558 backup safeguard takes the metadata-prefix fast path on normal
   grow-the-conversation saves and still produces a .bak on shrink.
3. Byte-identical re-saves skip the disk write entirely (no-op skip).
4. Composer drafts persist to a tiny sidecar file instead of rewriting the
   whole session JSON; sidecar wins over the legacy in-file field.
"""
from __future__ import annotations

import json
import os

import pytest

import api.config as _cfg
import api.models as _models
from api.models import (
    Session,
    composer_draft_sidecar_path,
    delete_composer_draft_sidecar,
    read_composer_draft_sidecar,
    resolve_composer_draft,
    write_composer_draft_sidecar,
)


@pytest.fixture
def session_store(tmp_path, monkeypatch):
    """Isolated SESSION_DIR + index + caches, mirroring test_session_duplicate_edit."""
    sessions_dir = tmp_path / "sessions"
    sessions_dir.mkdir()
    index_file = sessions_dir / "_index.json"
    # api.models imports SESSION_DIR/SESSION_INDEX_FILE at module level, so
    # patch BOTH api.config and api.models bindings.
    monkeypatch.setattr(_cfg, "SESSION_DIR", sessions_dir)
    monkeypatch.setattr(_models, "SESSION_DIR", sessions_dir)
    monkeypatch.setattr(_cfg, "SESSION_INDEX_FILE", index_file)
    monkeypatch.setattr(_models, "SESSION_INDEX_FILE", index_file)
    with _models.LOCK:
        _models.SESSIONS.clear()
    _models._DRAFT_SIDECAR_CACHE.clear()
    yield sessions_dir
    with _models.LOCK:
        _models.SESSIONS.clear()
    _models._DRAFT_SIDECAR_CACHE.clear()


def _msg(role, content, idx):
    return {"id": f"m{idx}", "role": role, "content": content, "timestamp": float(idx)}


def _make_session(sid, n_messages):
    s = Session(session_id=sid, title="hotpath test")
    s.messages = [_msg("user" if i % 2 == 0 else "assistant", f"msg {i}", i) for i in range(n_messages)]
    return s


def test_save_writes_compact_json_metadata_prefix_still_readable(session_store):
    s = _make_session("hotpath1", 4)
    s.save(skip_index=True)
    text = (session_store / "hotpath1.json").read_text(encoding="utf-8")
    # Compact output: no pretty-print newlines (escaped \n inside strings is fine).
    assert text.count("\n") == 0, "session JSON should be compact, not indented"
    parsed = json.loads(text)
    assert len(parsed["messages"]) == 4
    assert parsed["message_count"] == 4
    # The metadata-prefix scanner must still work on the compact layout.
    meta = Session.load_metadata_only("hotpath1")
    assert meta is not None
    assert getattr(meta, "_loaded_metadata_only", False) is True
    assert meta._metadata_message_count == 4
    assert meta.messages == []
    # And a full load round-trips.
    full = Session.load("hotpath1")
    assert len(full.messages) == 4


def test_backup_guard_grow_makes_no_bak_shrink_makes_bak(session_store):
    s = _make_session("hotpath2", 3)
    s.save(skip_index=True)
    s.messages.append(_msg("user", "grow", 3))
    s.save(skip_index=True)
    bak = session_store / "hotpath2.json.bak"
    assert not bak.exists(), "grow-the-conversation saves must not produce a backup"
    # Shrink → .bak snapshot of the pre-shrink state.
    s2 = Session.load("hotpath2")
    s2.messages = s2.messages[:1]
    s2.save(skip_index=True)
    assert bak.exists(), "a shrinking save must leave a recoverable .bak"
    assert len(json.loads(bak.read_text(encoding="utf-8"))["messages"]) == 4
    assert len(json.loads((session_store / "hotpath2.json").read_text(encoding="utf-8"))["messages"]) == 1


def test_backup_guard_refuses_empty_overwrite_with_active_stream(session_store):
    s = _make_session("hotpath3", 2)
    s.save(skip_index=True)
    stub = Session(session_id="hotpath3", title="stub")
    stub.messages = []
    stub.active_stream_id = "stream-x"
    stub.save(skip_index=True)
    on_disk = json.loads((session_store / "hotpath3.json").read_text(encoding="utf-8"))
    assert len(on_disk["messages"]) == 2, (
        "an empty active/pending snapshot must not overwrite a populated session"
    )


def test_noop_resave_skips_disk_write(session_store):
    s = _make_session("hotpath4", 2)
    s.save(skip_index=True)
    p = session_store / "hotpath4.json"
    st1 = os.stat(p)
    # Identical content, no updated_at touch → must not rewrite the file.
    s.save(touch_updated_at=False, skip_index=True)
    st2 = os.stat(p)
    assert (st1.st_mtime_ns, st1.st_size) == (st2.st_mtime_ns, st2.st_size), (
        "byte-identical re-save should skip the disk write"
    )
    # Real change → must write.
    s.title = "changed"
    s.save(touch_updated_at=False, skip_index=True)
    st3 = os.stat(p)
    assert st3.st_mtime_ns != st1.st_mtime_ns
    assert json.loads(p.read_text(encoding="utf-8"))["title"] == "changed"


def test_noop_skip_defers_to_external_writer(session_store):
    s = _make_session("hotpath5", 1)
    s.save(skip_index=True)
    p = session_store / "hotpath5.json"
    # Another writer replaces the file (different Session object / process).
    other = Session.load("hotpath5")
    other.title = "external"
    other.save(skip_index=True)
    # The original object's payload is unchanged, but the file on disk moved on:
    # the skip must not fire, and this save must win with its own content.
    s.save(touch_updated_at=False, skip_index=True)
    assert json.loads(p.read_text(encoding="utf-8"))["title"] == "hotpath test"


def test_draft_sidecar_roundtrip_and_precedence(session_store):
    sid = "hotpath6"
    s = _make_session(sid, 1)
    s.composer_draft = {"text": "legacy draft", "files": []}
    s.save(skip_index=True)
    # No sidecar yet → legacy field resolves.
    assert resolve_composer_draft(sid, s.composer_draft)["text"] == "legacy draft"
    # Sidecar write does NOT touch the session file.
    st1 = os.stat(session_store / f"{sid}.json")
    stored = write_composer_draft_sidecar(sid, {"text": "sidecar draft", "files": []})
    st2 = os.stat(session_store / f"{sid}.json")
    assert (st1.st_mtime_ns, st1.st_size) == (st2.st_mtime_ns, st2.st_size)
    assert stored["text"] == "sidecar draft"
    assert composer_draft_sidecar_path(sid).exists()
    assert read_composer_draft_sidecar(sid)["text"] == "sidecar draft"
    # Sidecar wins over legacy — including an EMPTY sidecar (cleared draft).
    assert resolve_composer_draft(sid, s.composer_draft)["text"] == "sidecar draft"
    write_composer_draft_sidecar(sid, {"text": "", "files": []})
    assert resolve_composer_draft(sid, s.composer_draft)["text"] == ""
    # compact() serves the sidecar value.
    assert s.compact()["composer_draft"]["text"] == ""
    # Delete → falls back to legacy again.
    delete_composer_draft_sidecar(sid)
    assert resolve_composer_draft(sid, s.composer_draft)["text"] == "legacy draft"


def test_draft_sidecar_rejects_unsafe_session_ids(session_store):
    assert composer_draft_sidecar_path("../evil") is None
    assert read_composer_draft_sidecar("../evil") is None
    with pytest.raises(ValueError):
        write_composer_draft_sidecar("../evil", {"text": "x"})


def test_draft_sidecar_not_picked_up_as_session(session_store):
    sid = "hotpath7"
    _make_session(sid, 1).save(skip_index=True)
    write_composer_draft_sidecar(sid, {"text": "draft"})
    top_level = {p.name for p in session_store.glob("*.json")}
    assert f"{sid}.json" in top_level
    assert not any("_drafts" in str(p) for p in session_store.glob("*.json")), (
        "draft sidecars must live outside the top-level *.json session scan"
    )
