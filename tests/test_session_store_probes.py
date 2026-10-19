"""Operational probes for the session store.

These pin the conditions a storage-backend change has to survive — they are not
new product behavior:

* a database written by a process that vanished without a clean shutdown still
  loads, and its durable cutover markers still authorize it;
* a sidecar write really does serialize against the migration publish window
  (the shared/exclusive lock protocol, not just the code path);
* a directory holding both representations resolves each session on its own, and
  a store row beats a leftover sidecar.
"""

import json
import os
import tempfile
import threading
import time
from collections import OrderedDict
from pathlib import Path

import api.models as models
from api import webui_session_sqlite as sqlite_db


def _tmp_session_dir() -> Path:
    return Path(tempfile.mkdtemp())


def _patch_state(monkeypatch, d: Path) -> None:
    import api.routes as routes

    sessions = OrderedDict()
    monkeypatch.setattr(models, "SESSION_DIR", d)
    monkeypatch.setattr(models, "SESSION_INDEX_FILE", d / "_index.json")
    monkeypatch.setattr(models, "SESSIONS", sessions)
    monkeypatch.setattr(models, "_sqlite_session_store_instance", None)
    monkeypatch.setattr(
        models, "_PERSISTED_SESSION_IDS_CACHE", (None, None, None, frozenset())
    )
    monkeypatch.setattr(routes, "SESSION_DIR", d)
    monkeypatch.setattr(routes, "SESSIONS", sessions)


def _sample(sid: str) -> dict:
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


def test_store_reloads_after_an_unclean_restart(monkeypatch):
    """A writer that dies without closing must not strand its sessions: the next
    process re-probes from disk, sees a durable cutover marker, and can keep
    writing."""
    d = _tmp_session_dir()
    _patch_state(monkeypatch, d)

    writer = sqlite_db.WebUISqliteSessionDB(session_dir=d)
    payload = _sample("sid-crash")
    payload["composer_draft"] = {"text": "half typed", "files": []}
    writer.write_session(payload)
    # No close(): the process disappears with the connection still open, leaving
    # whatever the WAL holds.
    del writer

    # Restart: nothing in-process may be consulted.
    monkeypatch.setattr(models, "_sqlite_session_store_instance", None)

    loaded = models.Session.load("sid-crash")
    assert loaded is not None, "the session vanished across the restart"
    assert [m["content"] for m in loaded.messages] == ["hello", "hi"]
    assert loaded.composer_draft["text"] == "half typed"

    store = models.get_session_store()
    assert store.supports_generation is True, "the cutover marker did not survive"

    loaded.title = "after restart"
    loaded.save(touch_updated_at=False, skip_index=True)
    reopened = sqlite_db.WebUISqliteSessionDB(session_dir=d)
    assert reopened.read_session("sid-crash")["title"] == "after restart"


def test_publish_window_blocks_sidecar_writes_until_release(monkeypatch):
    """The migration holds the cutover lock EXCLUSIVE while it publishes; a live
    sidecar write takes it SHARED. Prove that actually serializes, so an
    acknowledged save can never land inside the publish window."""
    fcntl = __import__("pytest").importorskip("fcntl")
    from api.session_store import CUTOVER_LOCK_NAME

    d = _tmp_session_dir()
    _patch_state(monkeypatch, d)

    s = models.Session(
        session_id="sid-publish",
        title="Before",
        messages=[{"role": "user", "content": "hi"}],
    )
    s.save(touch_updated_at=False, skip_index=True)
    assert (d / "sid-publish.json").exists()

    fd = os.open(d / CUTOVER_LOCK_NAME, os.O_CREAT | os.O_RDWR, 0o600)
    fcntl.flock(fd, fcntl.LOCK_EX)  # the publish window
    done = threading.Event()
    errors: list[BaseException] = []

    def _write():
        try:
            s.title = "During publish"
            s.save(touch_updated_at=False, skip_index=True)
        except BaseException as exc:  # surfaced by the assertions below
            errors.append(exc)
        finally:
            done.set()

    writer = threading.Thread(target=_write, daemon=True)
    writer.start()
    time.sleep(0.35)
    blocked = not done.is_set()

    fcntl.flock(fd, fcntl.LOCK_UN)
    os.close(fd)

    assert done.wait(15), "the save never completed after the window closed"
    writer.join(15)
    assert not errors, errors
    assert blocked, "the sidecar write ran inside the publish window"
    # Whatever it acknowledged is what is on disk.
    assert json.loads((d / "sid-publish.json").read_text())["title"] == "During publish"


def test_mixed_directory_resolves_each_session_independently(monkeypatch):
    """Partially migrated directories are supported: store-backed and
    sidecar-backed sessions load side by side, and a store row beats a leftover
    sidecar for the same sid."""
    d = _tmp_session_dir()
    _patch_state(monkeypatch, d)

    store = sqlite_db.WebUISqliteSessionDB(session_dir=d)
    payload = _sample("sid-sql")
    payload["title"] = "From SQL"
    store.write_session(payload)

    # A session that predates the migration: a sidecar with no store row. (A
    # NEW session saved while the store is active goes into the store — that is
    # why the unmigrated case has to be written as bytes.)
    sidecar_payload = _sample("sid-json")
    sidecar_payload["title"] = "From JSON"
    sidecar_payload["messages"] = [{"role": "user", "content": "hi", "timestamp": 1000.0}]
    (d / "sid-json.json").write_text(
        json.dumps(sidecar_payload, ensure_ascii=False), encoding="utf-8"
    )
    assert store.session_exists("sid-json") is False

    # A leftover sidecar for the migrated sid must not shadow the row.
    (d / "sid-sql.json").write_text(
        json.dumps(
            {
                "session_id": "sid-sql",
                "title": "Stale sidecar",
                "messages": [{"role": "user", "content": "old"}],
                "context_messages": [],
                "tool_calls": [],
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )

    models.SESSIONS.clear()
    assert models.Session.load("sid-sql").title == "From SQL"
    assert models.Session.load("sid-json").title == "From JSON"
    listed = {row["session_id"] for row in models.all_sessions()}
    assert {"sid-sql", "sid-json"} <= listed


def _drive_delete(monkeypatch, sid):
    """POST /api/session/delete through routes.handle_post, responders captured."""
    from types import SimpleNamespace

    import api.routes as routes

    captured = {}
    monkeypatch.setattr(routes, "_check_csrf", lambda handler: True)
    monkeypatch.setattr(routes, "read_body", lambda handler: {"session_id": sid})
    monkeypatch.setattr(
        routes,
        "bad",
        lambda handler, msg, status=400, **kw: captured.update(error=msg, status=status)
        or True,
    )
    monkeypatch.setattr(
        routes,
        "j",
        lambda handler, payload, status=200, **kw: captured.update(
            payload=payload, status=status
        )
        or True,
    )
    handler = SimpleNamespace(command="POST", _safe_webui_print=lambda *_a, **_k: None)
    assert routes.handle_post(
        handler, SimpleNamespace(path="/api/session/delete")
    ) is True
    return captured


def test_delete_route_clears_a_migrated_session_and_its_residuals(monkeypatch):
    """A migrated session has no sidecar, but its delete must still clear the
    residuals that DO exist (attachments, the .bak snapshot) and leave no durable
    retry lease or cleanup-phase row behind. This is the delete path a migrated
    install actually exercises."""
    import api.routes as routes
    import api.turn_journal as turn_journal
    import api.upload as upload

    d = _tmp_session_dir()
    _patch_state(monkeypatch, d)
    monkeypatch.setattr(routes, "SESSION_INDEX_FILE", d / "_index.json")
    monkeypatch.setattr(models, "delete_cli_session", lambda sid_: True)
    monkeypatch.setattr(upload, "_session_attachment_dir", lambda sid: d / "attachments" / sid)
    monkeypatch.setattr(turn_journal, "_default_session_dir", lambda: d)

    store = sqlite_db.WebUISqliteSessionDB(session_dir=d)
    store.write_session(_sample("sid-mig-del"))
    monkeypatch.setattr(models, "_sqlite_session_store_instance", store)

    attachments = d / "attachments" / "sid-mig-del"
    attachments.mkdir(parents=True)
    (attachments / "note.txt").write_text("payload", encoding="utf-8")
    backup = d / "sid-mig-del.json.bak"
    backup.write_text("{}", encoding="utf-8")
    assert not (d / "sid-mig-del.json").exists()

    captured = _drive_delete(monkeypatch, "sid-mig-del")

    assert captured.get("status") == 200, captured
    assert captured.get("payload", {}).get("ok") is True
    assert store.session_exists("sid-mig-del") is False
    assert not attachments.exists(), "attachment dir survived the delete"
    assert not backup.exists(), "the .bak snapshot survived the delete"
    assert store.list_cleanup_leases() == []
    assert store.list_cleanup_phases() == []
