"""Every durable writer of message arrays must bound inline image bytes.

`Session.save()` is not the only writer: recovery materializes sidecars from
state.db, the workspace-binding repair rewrites a whole sidecar, and the #1558
shrink guard copies the previous file to `.bak`. Each bypassed the save-boundary
projection, so a legacy (or state.db-sourced) transcript could re-enter disk with
full base64 payloads.
"""
from __future__ import annotations

import base64
import json
from collections import OrderedDict

import pytest


def _huge_part(chars=200_000):
    raw = base64.b64encode(b"\xff\xd8\xff\xe0" + b"A" * chars).decode()
    return {"type": "image_url", "image_url": {"url": "data:image/jpeg;base64," + raw}}


def test_project_message_payloads_is_copy_on_write():
    from api.session_image_persistence_override import project_message_payloads

    clean = {"session": {"messages": [{"role": "user", "content": "hi"}]}}
    assert project_message_payloads(clean) is clean

    # Display transcript is projected...
    dirty = {"session": {"messages": [{"role": "user", "content": [_huge_part(1000)]}]}}
    projected = project_message_payloads(dirty)
    assert projected is not dirty
    assert "data:image" not in json.dumps(projected)
    assert "data:image" in json.dumps(dirty)

    # ...the provider replay prefix is preserved exactly unless opted in.
    replay = {"session": {"context_messages": [{"role": "user", "content": [_huge_part(1000)]}]}}
    assert project_message_payloads(replay) is replay
    opt_in = project_message_payloads(replay, keys=("context_messages",))
    assert "data:image" not in json.dumps(opt_in)
    # copy-on-write: the caller's payload is untouched
    assert "data:image" in json.dumps(replay)


def test_recovery_materialization_bounds_image_bytes(tmp_path, monkeypatch):
    import api.session_recovery as recovery
    from api.session_image_persistence_override import project_message_payloads

    session_dir = tmp_path / "sessions"
    row = {
        "id": "rec-1",
        "messages": [{"role": "user", "content": [_huge_part(2000)]}],
    }
    monkeypatch.setattr(recovery, "_read_state_db_missing_sidecar_rows", lambda *a, **k: [row])
    monkeypatch.setattr(
        recovery,
        "_state_db_row_to_sidecar",
        lambda r: {"session_id": r["id"], "messages": r["messages"], "message_count": 1},
    )

    recovery.recover_missing_sidecars_from_state_db(session_dir, None)

    written = (session_dir / "rec-1.json").read_text()
    assert "data:image" not in written
    # Sanity: the projector is what removed it.
    assert "data:image" not in json.dumps(project_message_payloads(row))


def test_workspace_binding_rewrite_bounds_image_bytes(tmp_path, monkeypatch):
    import api.models as models

    sessions = tmp_path / "sessions"
    sessions.mkdir()
    monkeypatch.setattr(models, "SESSION_DIR", sessions)
    payload = {
        "session_id": "ws-1",
        "messages": [{"role": "user", "content": [_huge_part(2000)]}],
        "workspace": "/old/path",
    }
    (sessions / "ws-1.json").write_text(json.dumps(payload))
    session = models.Session(session_id="ws-1", workspace="/old/path")

    models.persist_recovered_workspace_binding(session, "/new/path")

    written = (sessions / "ws-1.json").read_text()
    assert "data:image" not in written
    assert json.loads(written)["workspace"] == "/new/path"


def test_shrink_backup_does_not_copy_legacy_image_bytes(tmp_path, monkeypatch):
    import api.models as models

    sessions = tmp_path / "sessions"
    sessions.mkdir()
    monkeypatch.setattr(models, "SESSION_DIR", sessions)
    monkeypatch.setattr(models, "SESSION_INDEX_FILE", sessions / "_index.json")
    monkeypatch.setattr(models, "SESSIONS", OrderedDict())

    legacy = {
        "session_id": "bak-1",
        "messages": [
            {"role": "user", "content": [_huge_part(2000)]},
            {"role": "assistant", "content": "one"},
            {"role": "assistant", "content": "two"},
        ],
        "message_count": 3,
    }
    (sessions / "bak-1.json").write_text(json.dumps(legacy))

    # Shrinking the transcript triggers the #1558 backup path.
    smaller = models.Session(session_id="bak-1", messages=[{"role": "user", "content": "trimmed"}])
    smaller.save(touch_updated_at=False)

    bak = sessions / "bak-1.json.bak"
    assert bak.exists(), "expected a shrink backup"
    assert "data:image" not in bak.read_text()
    assert "data:image" not in (sessions / "bak-1.json").read_text()
