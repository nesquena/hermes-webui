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


def test_session_delete_removes_primary_bak_and_torn_created_by_save(
    session_dir, monkeypatch
):
    """#8807: /api/session/delete must remove the exact .json.torn salvage
    Session.save() parks beside a kept readable backup. Avoid prefix globs;
    only the known sibling suffix is cleaned under the session mutation lock.
    """
    from types import SimpleNamespace

    import api.models as models
    import api.routes as routes

    sid = "s_8798_delete_torn"
    path, bak, good_bak, cut = _grow_cut_with_bak10(session_dir, sid)
    _session(sid, _messages(9, word="kept")).save(skip_index=True)

    torn = path.with_suffix(".json.torn")
    assert path.exists()
    assert bak.read_bytes() == good_bak
    assert torn.read_bytes() == cut

    # Ordinary-delete control sibling that must survive.
    other = session_dir / "s_8798_keep.json"
    other.write_text('{"messages": []}', encoding="utf-8")
    other_bak = other.with_suffix(".json.bak")
    other_bak.write_text("keep-bak", encoding="utf-8")

    monkeypatch.setattr(routes, "SESSION_DIR", session_dir)
    monkeypatch.setattr(routes, "SESSION_INDEX_FILE", session_dir / "_index.json")
    monkeypatch.setattr(routes, "_check_csrf", lambda handler: True)
    monkeypatch.setattr(routes, "read_body", lambda handler: {"session_id": sid})
    monkeypatch.setattr(routes, "_lookup_cli_session_metadata", lambda value: {})
    monkeypatch.setattr(routes, "_session_is_subagent_view_only", lambda value: False)
    monkeypatch.setattr(routes, "_is_messaging_session_id", lambda value: False)
    monkeypatch.setattr(
        routes, "_worktree_retained_payload_for_session_id", lambda value: {}
    )
    monkeypatch.setattr(routes, "prune_session_from_index", lambda value: None)
    monkeypatch.setattr(
        routes, "_record_webui_deleted_session_tombstone", lambda value: None
    )
    monkeypatch.setattr(
        routes, "_publish_session_list_changed", lambda *a, **k: None
    )
    monkeypatch.setattr(models, "delete_cli_session", lambda value: True)

    captured = {}

    def _capture(handler, payload, status=200, extra_headers=None):
        captured.update(payload=payload, status=status)
        return True

    monkeypatch.setattr(routes, "j", _capture)
    # Eviction / journal / attachment cleanup must not depend on real dirs.
    import api.config as config_module
    import api.upload as upload_module
    import api.turn_journal as turn_journal_module
    import api.run_journal as run_journal_module
    import api.background_process as background_module

    monkeypatch.setattr(config_module, "_evict_session_agent", lambda value: None)
    monkeypatch.setattr(
        upload_module, "_session_attachment_dir", lambda value: session_dir / "att" / value
    )
    monkeypatch.setattr(turn_journal_module, "delete_turn_journal", lambda value: None)
    monkeypatch.setattr(run_journal_module, "delete_run_journal", lambda value: None)
    monkeypatch.setattr(
        background_module, "forget_bg_task_completion_dedup", lambda value: None
    )

    assert routes.handle_post(object(), SimpleNamespace(path="/api/session/delete")) is True
    assert captured["status"] == 200
    assert captured["payload"]["ok"] is True

    assert not path.exists()
    assert not bak.exists()
    assert not torn.exists(), "save-created .json.torn must leave with the session"
    assert other.exists() and other_bak.exists()


def test_ordinary_session_delete_without_torn_still_clears_primary_and_bak(
    session_dir, monkeypatch
):
    """Ordinary-delete control: no torn sibling, primary + bak still removed."""
    from types import SimpleNamespace

    import api.models as models
    import api.routes as routes

    sid = "s_8798_delete_ordinary"
    _session(sid, _messages(6)).save(skip_index=True)
    _session(sid, _messages(2)).save(skip_index=True)
    path = session_dir / f"{sid}.json"
    bak = path.with_suffix(".json.bak")
    assert path.exists() and bak.exists()
    assert not path.with_suffix(".json.torn").exists()

    monkeypatch.setattr(routes, "SESSION_DIR", session_dir)
    monkeypatch.setattr(routes, "SESSION_INDEX_FILE", session_dir / "_index.json")
    monkeypatch.setattr(routes, "_check_csrf", lambda handler: True)
    monkeypatch.setattr(routes, "read_body", lambda handler: {"session_id": sid})
    monkeypatch.setattr(routes, "_lookup_cli_session_metadata", lambda value: {})
    monkeypatch.setattr(routes, "_session_is_subagent_view_only", lambda value: False)
    monkeypatch.setattr(routes, "_is_messaging_session_id", lambda value: False)
    monkeypatch.setattr(
        routes, "_worktree_retained_payload_for_session_id", lambda value: {}
    )
    monkeypatch.setattr(routes, "prune_session_from_index", lambda value: None)
    monkeypatch.setattr(
        routes, "_record_webui_deleted_session_tombstone", lambda value: None
    )
    monkeypatch.setattr(
        routes, "_publish_session_list_changed", lambda *a, **k: None
    )
    monkeypatch.setattr(models, "delete_cli_session", lambda value: True)

    captured = {}
    monkeypatch.setattr(
        routes,
        "j",
        lambda handler, payload, status=200, extra_headers=None: captured.update(
            payload=payload, status=status
        )
        or True,
    )
    import api.config as config_module
    import api.upload as upload_module
    import api.turn_journal as turn_journal_module
    import api.run_journal as run_journal_module
    import api.background_process as background_module

    monkeypatch.setattr(config_module, "_evict_session_agent", lambda value: None)
    monkeypatch.setattr(
        upload_module, "_session_attachment_dir", lambda value: session_dir / "att" / value
    )
    monkeypatch.setattr(turn_journal_module, "delete_turn_journal", lambda value: None)
    monkeypatch.setattr(run_journal_module, "delete_run_journal", lambda value: None)
    monkeypatch.setattr(
        background_module, "forget_bg_task_completion_dedup", lambda value: None
    )

    assert routes.handle_post(object(), SimpleNamespace(path="/api/session/delete")) is True
    assert captured["status"] == 200
    assert captured["payload"]["ok"] is True
    assert not path.exists()
    assert not bak.exists()


def _patch_session_delete_route(monkeypatch, session_dir, sid):
    """Wire /api/session/delete to the temp session_dir for focused delete tests."""
    from types import SimpleNamespace

    import api.models as models
    import api.routes as routes
    import api.config as config_module
    import api.upload as upload_module
    import api.turn_journal as turn_journal_module
    import api.run_journal as run_journal_module
    import api.background_process as background_module

    monkeypatch.setattr(routes, "SESSION_DIR", session_dir)
    monkeypatch.setattr(routes, "SESSION_INDEX_FILE", session_dir / "_index.json")
    monkeypatch.setattr(routes, "_check_csrf", lambda handler: True)
    monkeypatch.setattr(routes, "read_body", lambda handler: {"session_id": sid})
    monkeypatch.setattr(routes, "_lookup_cli_session_metadata", lambda value: {})
    monkeypatch.setattr(routes, "_session_is_subagent_view_only", lambda value: False)
    monkeypatch.setattr(routes, "_is_messaging_session_id", lambda value: False)
    monkeypatch.setattr(
        routes, "_worktree_retained_payload_for_session_id", lambda value: {}
    )
    monkeypatch.setattr(routes, "prune_session_from_index", lambda value: None)
    monkeypatch.setattr(
        routes, "_record_webui_deleted_session_tombstone", lambda value: None
    )
    monkeypatch.setattr(
        routes, "_publish_session_list_changed", lambda *a, **k: None
    )
    monkeypatch.setattr(models, "delete_cli_session", lambda value: True)
    monkeypatch.setattr(config_module, "_evict_session_agent", lambda value: None)
    monkeypatch.setattr(
        upload_module, "_session_attachment_dir", lambda value: session_dir / "att" / value
    )
    monkeypatch.setattr(turn_journal_module, "delete_turn_journal", lambda value: None)
    monkeypatch.setattr(run_journal_module, "delete_run_journal", lambda value: None)
    monkeypatch.setattr(
        background_module, "forget_bg_task_completion_dedup", lambda value: None
    )

    captured = {}

    def _capture(handler, payload, status=200, extra_headers=None):
        captured.update(payload=payload, status=status)
        return True

    monkeypatch.setattr(routes, "j", _capture)
    return routes, captured, SimpleNamespace


def test_session_delete_removes_retained_torn_salvage_temp(session_dir, monkeypatch):
    """#8807 R1: producer may leave `<sid>.json.torn.tmp.<pid>.<thread>` when
    salvage rename/cleanup is denied. Accepted deletion must unlink that exact
    grammar under the session mutation lock, without touching a neighboring
    session's similarly named temp.
    """
    import os
    import threading

    import api.models as models

    sid = "s_8798_delete_torn_tmp"
    path, bak, good_bak, cut = _grow_cut_with_bak10(session_dir, sid)

    # Real producer path: deny salvage publish + temp cleanup so the temp stays.
    real_replace = models._safe_replace
    real_unlink = type(path).unlink
    deny = {"replace": 0, "cleanup": 0}

    def _deny_salvage_replace(src, dst):
        src_name = getattr(src, "name", str(src))
        if ".json.torn.tmp." in src_name:
            deny["replace"] += 1
            raise OSError("injected salvage replace denial")
        return real_replace(src, dst)

    def _deny_torn_tmp_unlink(self, *args, **kwargs):
        if ".json.torn.tmp." in self.name:
            deny["cleanup"] += 1
            raise OSError("injected salvage temp cleanup denial")
        return real_unlink(self, *args, **kwargs)

    monkeypatch.setattr(models, "_safe_replace", _deny_salvage_replace)
    monkeypatch.setattr(type(path), "unlink", _deny_torn_tmp_unlink)

    _session(sid, _messages(9, word="kept")).save(skip_index=True)

    # Restore normal unlink/replace for the delete path under test.
    monkeypatch.setattr(models, "_safe_replace", real_replace)
    monkeypatch.setattr(type(path), "unlink", real_unlink)

    temps = [
        p
        for p in session_dir.iterdir()
        if p.is_file() and p.name.startswith(f"{sid}.json.torn.tmp.")
    ]
    assert deny["replace"] == 1
    assert len(temps) == 1, f"expected one retained salvage temp, got {temps}"
    retained = temps[0]
    assert retained.read_bytes() == cut

    # Neighboring session id with the same producer grammar must survive.
    neighbor_sid = "s_8798_delete_torn_tmp_neighbor"
    neighbor_tmp = session_dir / (
        f"{neighbor_sid}.json.torn.tmp.{os.getpid()}.{threading.current_thread().ident}"
    )
    neighbor_tmp.write_bytes(b"neighbor-salvage-temp")
    # Also plant final .torn for the target so ordinary cleanup still runs.
    torn = path.with_suffix(".json.torn")
    # Producer never published final torn under the denial; plant one too.
    if not torn.exists():
        torn.write_bytes(cut)

    routes, captured, SimpleNamespace = _patch_session_delete_route(
        monkeypatch, session_dir, sid
    )
    assert routes.handle_post(object(), SimpleNamespace(path="/api/session/delete")) is True
    assert captured["status"] == 200
    assert captured["payload"]["ok"] is True

    assert not path.exists()
    assert not bak.exists()
    assert not torn.exists()
    assert not retained.exists(), "retained salvage temp must leave with the session"
    assert neighbor_tmp.exists() and neighbor_tmp.read_bytes() == b"neighbor-salvage-temp"


def _write_unmarked_torn_sidecar(path, n=4):
    """Legacy-shaped sidecar (no _mc_v) cut mid-UTF-8 so prefix count is None
    and the full-read fallback hits UnicodeDecodeError → existing_msg_count=-1.
    """
    from api.models import _prefix_message_count

    payload = {
        "title": "legacy UTF-8 session",
        "workspace": "",
        "model": "test-model",
        "created_at": 1.0,
        "updated_at": 2.0,
        "messages": _messages(n, word="héllo ✓"),
        # deliberately omit _mc_v and message_count
    }
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    assert _prefix_message_count(path) is None, "fixture must be unmarked"
    cut = _cut_mid_character(path)
    assert _prefix_message_count(path) is None
    return cut


def test_unmarked_torn_sidecar_empty_active_save_preserves_primary(session_dir):
    """#8807 R2: missing-marker torn primary + empty active snapshot must not
    publish over the undecodable bytes just because the count is unknown (-1).
    """
    from api.models import _prefix_message_count

    sid = "s_8798_unmarked_active"
    path = session_dir / f"{sid}.json"
    cut = _write_unmarked_torn_sidecar(path)
    assert _prefix_message_count(path) is None

    empty_active = _session(sid, [])
    empty_active.active_stream_id = "still-running"
    empty_active.save(skip_index=True)

    assert path.read_bytes() == cut, "torn primary must stay when count is unknown"


def test_unmarked_torn_sidecar_empty_pending_save_preserves_primary(session_dir):
    """#8807 R2: missing-marker torn primary + empty pending snapshot must keep
    the raw bytes (production pending_user_message / pending_started_at shape).
    """
    sid = "s_8798_unmarked_pending"
    path = session_dir / f"{sid}.json"
    cut = _write_unmarked_torn_sidecar(path)

    empty_pending = _session(sid, [])
    empty_pending.pending_user_message = "queued prompt"
    empty_pending.pending_started_at = 123.0
    empty_pending.save(skip_index=True)

    assert path.read_bytes() == cut, "torn primary must stay for empty pending"


def test_marked_torn_empty_active_still_preserves_and_unmarked_nonempty_may_save(
    session_dir,
):
    """Controls: marked + empty active still refuses; unmarked torn + nonempty
    incoming may still replace (nonempty fallback is not a certified regression).
    """
    from api.models import _prefix_message_count

    # Marked control: normal save then cut, empty active refuses.
    sid_marked = "s_8798_marked_active"
    _session(sid_marked, _messages(5)).save(skip_index=True)
    path_marked = session_dir / f"{sid_marked}.json"
    cut_marked = _cut_mid_character(path_marked)
    assert _prefix_message_count(path_marked) == 5
    empty = _session(sid_marked, [])
    empty.active_stream_id = "stream"
    empty.save(skip_index=True)
    assert path_marked.read_bytes() == cut_marked

    # Nonempty unmarked control: a real nonempty snapshot may overwrite.
    sid_nonempty = "s_8798_unmarked_nonempty"
    path_nonempty = session_dir / f"{sid_nonempty}.json"
    _write_unmarked_torn_sidecar(path_nonempty, n=3)
    _session(sid_nonempty, _messages(2, word="kept")).save(skip_index=True)
    data = _reload(path_nonempty)
    assert [m["content"] for m in data["messages"]] == ["kept 0", "kept 1"]
