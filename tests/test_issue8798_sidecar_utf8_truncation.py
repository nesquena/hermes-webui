"""#8798: a sidecar cut inside a multi-byte UTF-8 character must not stop a
valid in-memory session from saving.

Two shapes reach the shrink-backup read in Session.save():
  * the 64 KiB prefix still holds a readable message_count, but the body is
    cut mid-character: the shrink backup reads the whole file;
  * the prefix itself is unreadable, so save() falls back to a full read.
Before the fix both raised UnicodeDecodeError and the valid snapshot was never
written.
"""
import json
from collections import OrderedDict

import pytest


@pytest.fixture
def session_dir(tmp_path, monkeypatch):
    import api.models as models

    sd = tmp_path / "sessions"
    sd.mkdir()
    monkeypatch.setattr(models, "SESSION_DIR", sd)
    monkeypatch.setattr(models, "SESSIONS", OrderedDict())
    return sd


def _messages(n, word="héllo ✓"):
    return [
        {"role": "user" if i % 2 == 0 else "assistant", "content": f"{word} {i}"}
        for i in range(n)
    ]


def _session(sid, messages):
    from api.models import Session

    return Session(
        session_id=sid,
        title="UTF-8 session",
        workspace="",
        model="test-model",
        created_at=1.0,
        updated_at=2.0,
        messages=messages,
    )


def _cut_mid_character(path):
    """Truncate the sidecar inside the last multi-byte character."""
    raw = path.read_bytes()
    marker = "✓".encode("utf-8")  # 3 bytes
    at = raw.rfind(marker)
    assert at > 0
    path.write_bytes(raw[: at + 1])
    with pytest.raises(UnicodeDecodeError):
        path.read_text(encoding="utf-8")
    return raw[: at + 1]


def _reload(path):
    return json.loads(path.read_text(encoding="utf-8"))


def test_shrink_save_with_readable_prefix_saves_and_backs_up_exact_bytes(session_dir):
    from api.models import _prefix_message_count

    sid = "s_8798_prefix"
    _session(sid, _messages(8)).save(skip_index=True)
    path = session_dir / f"{sid}.json"
    cut = _cut_mid_character(path)
    assert _prefix_message_count(path) == 8, "fixture must keep the prefix count readable"

    valid = _session(sid, _messages(3, word="kept"))
    valid.save(skip_index=True)

    data = _reload(path)
    assert [m["content"] for m in data["messages"]] == ["kept 0", "kept 1", "kept 2"]
    assert data["title"] == "UTF-8 session"
    # Torn bytes are parked on a non-recovery sibling, not on .json.bak.
    assert not path.with_suffix(".json.bak").exists()
    assert path.with_suffix(".json.torn").read_bytes() == cut


def test_full_read_fallback_treats_undecodable_sidecar_like_malformed_json(session_dir):
    sid = "s_8798_full"
    _session(sid, _messages(4)).save(skip_index=True)
    path = session_dir / f"{sid}.json"
    # No usable prefix count: the whole file is one cut multi-byte run.
    path.write_bytes("{\"title\": \"✓".encode("utf-8")[:-1])
    bak = path.with_suffix(".json.bak")
    bak.write_text("{\"messages\": [], \"note\": \"older backup\"}", encoding="utf-8")

    _session(sid, _messages(2, word="kept")).save(skip_index=True)

    assert [m["content"] for m in _reload(path)["messages"]] == ["kept 0", "kept 1"]
    # Same as malformed JSON: no shrink is proven, so an existing backup is left alone.
    assert json.loads(bak.read_text(encoding="utf-8"))["note"] == "older backup"


def test_malformed_json_and_valid_utf8_controls_unchanged(session_dir):
    sid = "s_8798_ctrl"
    _session(sid, _messages(6)).save(skip_index=True)
    path = session_dir / f"{sid}.json"

    # Ordinary UTF-8 shrink: backup is the old text.
    old_text = path.read_text(encoding="utf-8")
    _session(sid, _messages(2)).save(skip_index=True)
    assert path.with_suffix(".json.bak").read_text(encoding="utf-8") == old_text
    assert len(_reload(path)["messages"]) == 2

    # Malformed (but decodable) JSON still saves.
    path.write_text("{not json", encoding="utf-8")
    _session(sid, _messages(1)).save(skip_index=True)
    assert len(_reload(path)["messages"]) == 1


def test_torn_shrink_keeps_an_existing_usable_backup(session_dir):
    from api.models import _prefix_message_count
    from api.session_recovery import inspect_session_recovery_status

    sid = "s_8798_keep_bak"
    _session(sid, _messages(10)).save(skip_index=True)
    _session(sid, _messages(8)).save(skip_index=True)  # normal 10 -> 8 shrink
    path = session_dir / f"{sid}.json"
    bak = path.with_suffix(".json.bak")
    good_bak = bak.read_bytes()
    assert len(json.loads(good_bak)["messages"]) == 10

    _cut_mid_character(path)
    assert _prefix_message_count(path) == 8, "fixture must keep the prefix count readable"
    _session(sid, _messages(3, word="kept")).save(skip_index=True)

    data = _reload(path)
    assert [m["content"] for m in data["messages"]] == ["kept 0", "kept 1", "kept 2"]
    assert data["title"] == "UTF-8 session"
    # The 10-message backup is still there and still restorable.
    assert bak.read_bytes() == good_bak
    assert path.with_suffix(".json.torn").exists(), "torn sidecar should be salvaged aside"
    status = inspect_session_recovery_status(path)
    assert status["live_messages"] == 3
    assert status["bak_messages"] == 10
    assert status["recommend"] == "restore"


def test_keeping_a_backup_never_renames_a_stale_backup_temp(session_dir):
    """A temp left by an earlier failed backup (rename and cleanup both
    denied) must not replace the usable backup this save says it kept."""
    import os
    import threading

    sid = "s_8798_stale_tmp"
    _session(sid, _messages(10)).save(skip_index=True)
    _session(sid, _messages(8)).save(skip_index=True)  # backup now has 10
    path = session_dir / f"{sid}.json"
    bak = path.with_suffix(".json.bak")
    good_bak = bak.read_bytes()

    # Valid 8-message leftover under this process/thread's temp name.
    stale = bak.with_suffix(f".bak.tmp.{os.getpid()}.{threading.current_thread().ident}")
    stale.write_bytes(path.read_bytes())

    _cut_mid_character(path)
    _session(sid, _messages(3, word="kept")).save(skip_index=True)

    assert [m["content"] for m in _reload(path)["messages"]] == ["kept 0", "kept 1", "kept 2"]
    assert bak.read_bytes() == good_bak, "stale temp must not demote the 10-message backup"


def _grow_cut_with_bak10(session_dir, sid):
    """Save 10 → 8 (bak=10), grow primary to 12, cut mid-UTF-8 (prefix=12)."""
    from api.models import _prefix_message_count

    _session(sid, _messages(10)).save(skip_index=True)
    _session(sid, _messages(8)).save(skip_index=True)
    path = session_dir / f"{sid}.json"
    bak = path.with_suffix(".json.bak")
    good_bak = bak.read_bytes()
    assert len(json.loads(good_bak)["messages"]) == 10

    _session(sid, _messages(12)).save(skip_index=True)
    # Grow does not shrink, so bak stays the 10-message copy.
    assert bak.read_bytes() == good_bak

    cut = _cut_mid_character(path)
    assert _prefix_message_count(path) == 12
    return path, bak, good_bak, cut


@pytest.mark.parametrize("incoming_count", [9, 10, 11])
def test_primary_failure_keeps_readable_bak_for_below_equal_above_incoming(
    session_dir, monkeypatch, incoming_count
):
    """#8807 P1: a failed primary publish must not have already replaced a
    readable .json.bak with undecodable torn bytes, whether the backup count
    is below, equal to, or above the uncommitted incoming snapshot."""
    import api.models as models
    from api.session_recovery import inspect_session_recovery_status

    sid = f"s_8798_pfail_{incoming_count}"
    path, bak, good_bak, _cut = _grow_cut_with_bak10(session_dir, sid)

    def _boom(tmp, dst, session_id):
        raise OSError("injected primary replace failure")

    monkeypatch.setattr(models, "_replace_session_sidecar", _boom)

    with pytest.raises(OSError, match="injected primary replace failure"):
        _session(sid, _messages(incoming_count, word="kept")).save(skip_index=True)

    # Primary still torn; recovery backup still the readable 10-message copy.
    with pytest.raises(UnicodeDecodeError):
        path.read_text(encoding="utf-8")
    assert bak.read_bytes() == good_bak
    status = inspect_session_recovery_status(path)
    assert status["bak_messages"] == 10
    assert status["recommend"] == "restore"


@pytest.mark.parametrize("incoming_count", [9, 10, 11])
def test_no_fault_primary_publishes_and_keeps_readable_bak(session_dir, incoming_count):
    """Matching no-fault controls: primary lands, readable bak stays at 10."""
    from api.session_recovery import inspect_session_recovery_status

    sid = f"s_8798_ok_{incoming_count}"
    path, bak, good_bak, _cut = _grow_cut_with_bak10(session_dir, sid)

    _session(sid, _messages(incoming_count, word="kept")).save(skip_index=True)

    data = _reload(path)
    assert len(data["messages"]) == incoming_count
    assert [m["content"] for m in data["messages"]] == [f"kept {i}" for i in range(incoming_count)]
    assert bak.read_bytes() == good_bak
    status = inspect_session_recovery_status(path)
    assert status["live_messages"] == incoming_count
    assert status["bak_messages"] == 10
