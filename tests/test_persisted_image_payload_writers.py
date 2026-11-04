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
from pathlib import Path

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


def _write_legacy_sidecar(sessions, sid, rows):
    (sessions / f"{sid}.json").write_text(json.dumps({
        "session_id": sid, "message_count": len(rows), "messages": rows,
        "tool_calls": [], "anchor_activity_scenes": {}, "workspace": "/old/path",
    }))


def _cold_cycle_counts(sessions, sid, cycles=3):
    import api.models as models
    session = models.Session.load(sid)
    counts = []
    for _ in range(cycles):
        session.save(touch_updated_at=False)
        counts.append(json.loads((sessions / f"{sid}.json").read_text())["message_count"])
        session = models.Session.load(sid)
    return counts


def _distinct_image_rows():
    import base64 as _b64

    url = "data:image/jpeg;base64," + _b64.b64encode(b"payload").decode()
    identity = {"role": "user", "id": "writer-detail", "timestamp": 50.5}
    return [
        {**identity, "content": [{"type": "image_url", "image_url": {"url": url, "detail": "low"}}]},
        {**identity, "content": [{"type": "image_url", "image_url": {"url": url, "detail": "high"}}]},
    ]


def test_workspace_rewrite_preserves_distinct_rows_across_cold_saves(tmp_path, monkeypatch):
    """The workspace writer rewrites the whole sidecar; it must keep the contract."""
    import api.models as models

    sessions = tmp_path / "sessions"
    sessions.mkdir()
    monkeypatch.setattr(models, "SESSION_DIR", sessions)
    _write_legacy_sidecar(sessions, "ws-cycle", _distinct_image_rows())

    session = models.Session.load("ws-cycle")
    models.persist_recovered_workspace_binding(session, "/new/path")

    rewritten = json.loads((sessions / "ws-cycle.json").read_text())
    assert len(rewritten["messages"]) == 2
    assert rewritten["workspace"] == "/new/path"
    assert _cold_cycle_counts(sessions, "ws-cycle") == [2, 2, 2]


def test_migration_keeps_distinct_rows_after_a_later_cold_save(tmp_path, monkeypatch):
    import importlib.util
    import api.models as models

    sessions = tmp_path / "sessions"
    sessions.mkdir()
    monkeypatch.setattr(models, "SESSION_DIR", sessions)
    _write_legacy_sidecar(sessions, "mig-cycle", _distinct_image_rows())

    script_path = Path(__file__).resolve().parent.parent / "scripts" / "migrate_persisted_image_payloads.py"
    spec = importlib.util.spec_from_file_location("mig_payloads", script_path)
    script = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(script)
    assert script.main(["--session-dir", str(sessions), "--apply"]) == 0

    assert _cold_cycle_counts(sessions, "mig-cycle") == [2, 2, 2]


def test_recovery_materialization_preserves_distinct_rows_across_cold_saves(tmp_path, monkeypatch):
    import api.models as models
    import api.session_recovery as recovery

    sessions = tmp_path / "sessions"
    sessions.mkdir()
    monkeypatch.setattr(models, "SESSION_DIR", sessions)
    rows = _distinct_image_rows()
    monkeypatch.setattr(recovery, "_read_state_db_missing_sidecar_rows", lambda *a, **k: [
        {"id": "rec-cycle", "messages": rows},
    ])
    monkeypatch.setattr(
        recovery, "_state_db_row_to_sidecar",
        lambda r: {"session_id": r["id"], "messages": r["messages"], "message_count": len(r["messages"])},
    )

    recovery.recover_missing_sidecars_from_state_db(sessions, None)

    assert _cold_cycle_counts(sessions, "rec-cycle") == [2, 2, 2]


# --- Writer-entry-point mirror preflight (maintainer: "Reuse parsed-shape
# recognition and test these writer entry points. The direct-save mirror-only
# test does not exercise this early-out.") ---

def _mirror_only_row(identity, mirror_string):
    """Display content is plain text; the ONLY inline image lives in the mirror."""
    return {**identity, "content": "plain display text", "api_content": mirror_string}


def _anthropic_mirror(raw: bytes) -> str:
    import base64 as _b64
    return json.dumps([{
        "type": "image",
        "source": {
            "type": "base64",
            "media_type": "image/png",
            "data": _b64.b64encode(raw).decode("ascii"),
        },
    }])


def _case_variant_mirror(raw: bytes) -> str:
    import base64 as _b64
    return json.dumps([{
        "type": "image_url",
        "image_url": {"url": "DATA:IMAGE/JPEG;BASE64," + _b64.b64encode(raw).decode("ascii")},
    }])


def _json_escaped_mirror(raw: bytes) -> str:
    """A legal JSON escape of the slash: parses to a real data URL, but the RAW
    string never contains the literal `data:image/` the old scan looked for."""
    import base64 as _b64
    return ('[{"type":"image_url","image_url":{"url":"data:image\\/jpeg;base64,'
            + _b64.b64encode(raw).decode("ascii") + '"}}]')


PREFLIGHT_MIRRORS = [
    ("anthropic-base64", _anthropic_mirror),
    ("case-variant", _case_variant_mirror),
    ("json-escaped", _json_escaped_mirror),
]


@pytest.mark.parametrize("label,builder", PREFLIGHT_MIRRORS)
def test_workspace_rewrite_bounds_mirror_only_shapes(tmp_path, monkeypatch, label, builder):
    """The workspace writer must reach mirror-only images in every accepted shape."""
    import api.models as models

    sessions = tmp_path / "sessions"
    sessions.mkdir()
    monkeypatch.setattr(models, "SESSION_DIR", sessions)
    identity = {"role": "user", "id": f"ws-{label}", "timestamp": 70.5}
    _write_legacy_sidecar(sessions, f"ws-{label}", [_mirror_only_row(identity, builder(b"m" * 64))])

    session = models.Session.load(f"ws-{label}")
    models.persist_recovered_workspace_binding(session, "/new/path")

    rewritten = (sessions / f"ws-{label}.json").read_text()
    persisted = json.loads(rewritten)
    assert "base64," not in persisted["messages"][0]["api_content"], f"{label} mirror leaked bytes"
    assert json.loads(persisted["messages"][0]["api_content"]) == [{"type": "text", "text": "[image attachment]"}]


@pytest.mark.parametrize("label,builder", PREFLIGHT_MIRRORS)
def test_migration_bounds_mirror_only_shapes(tmp_path, monkeypatch, label, builder):
    import importlib.util
    import api.models as models

    sessions = tmp_path / "sessions"
    sessions.mkdir()
    monkeypatch.setattr(models, "SESSION_DIR", sessions)
    identity = {"role": "user", "id": f"mig-{label}", "timestamp": 71.5}
    _write_legacy_sidecar(sessions, f"mig-{label}", [_mirror_only_row(identity, builder(b"m" * 64))])

    script_path = Path(__file__).resolve().parent.parent / "scripts" / "migrate_persisted_image_payloads.py"
    spec = importlib.util.spec_from_file_location("mig_payloads_preflight", script_path)
    script = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(script)
    assert script.main(["--session-dir", str(sessions), "--apply"]) == 0

    persisted = json.loads((sessions / f"mig-{label}.json").read_text())
    assert json.loads(persisted["messages"][0]["api_content"]) == [{"type": "text", "text": "[image attachment]"}]


def _make_real_state_db(path, *, sid, role, content_value, source="webui"):
    """Create a REAL sqlite state.db row -- no mocks, as the reviewer required."""
    import sqlite3

    conn = sqlite3.connect(path)
    try:
        conn.execute(
            "CREATE TABLE sessions (id TEXT PRIMARY KEY, source TEXT, title TEXT, "
            "model TEXT, started_at REAL, message_count INTEGER)"
        )
        conn.execute(
            "CREATE TABLE messages (id TEXT, session_id TEXT, role TEXT, "
            "content TEXT, timestamp REAL)"
        )
        conn.execute(
            "INSERT INTO sessions (id, source, title, model, started_at, message_count) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            (sid, source, "Real row", "test-model", 1.0, 1),
        )
        conn.execute(
            "INSERT INTO messages (id, session_id, role, content, timestamp) "
            "VALUES (?, ?, ?, ?, ?)",
            (f"{sid}-m0", sid, role, content_value, 10.0),
        )
        conn.commit()
    finally:
        conn.close()


_STATE_DB_PREFIX = "\x00json:"


@pytest.mark.parametrize("role", ["user", "tool"])
def test_recovery_bounds_a_real_sqlite_state_db_row(tmp_path, role):
    """Both roles must be bounded through a REAL state.db row.

    The reviewer's point: the recovery writer copies `messages.content` as the
    Agent's `\\x00json:` string, so the projection never saw list-valued parts and
    a real SQLite row retained the complete data URL. Mocking decoded lists hid
    this. This builds the actual sqlite representation instead.
    """
    import api.session_recovery as recovery

    session_dir = tmp_path / "sessions"
    session_dir.mkdir()
    db = tmp_path / "state.db"
    parts = [{"type": "text", "text": "hi"}, _huge_part(4000)]
    _make_real_state_db(db, sid="real-rec", role=role, content_value=_STATE_DB_PREFIX + json.dumps(parts))

    recovery.recover_missing_sidecars_from_state_db(session_dir, db)

    written = (session_dir / "real-rec.json").read_text()
    assert "data:image" not in written, f"role={role}: real SQLite row was NOT bounded"
    # Identity survives.
    payload = json.loads(written)
    assert payload.get("message_count") == 1
    assert any(m.get("role") == role for m in payload.get("messages", []))
    # The surrounding text is preserved, not blanked.
    assert "hi" in written


@pytest.mark.parametrize("role", ["user", "tool"])
def test_discoverability_writer_bounds_a_real_sqlite_state_db_row(tmp_path, role):
    """The discoverability repair writer needs the same real-row guarantee.

    `_materialize_sidecar_from_state_db` is the discoverability writer that
    bypasses Session.save() and calls the projection directly.
    """
    import api.session_discoverability as disc

    session_dir = tmp_path / "sessions"
    session_dir.mkdir()
    db = tmp_path / "state.db"
    parts = [{"type": "text", "text": "hi"}, _huge_part(4000)]
    _make_real_state_db(db, sid="real-disc", role=role, content_value=_STATE_DB_PREFIX + json.dumps(parts))

    result = disc._materialize_sidecar_from_state_db(
        session_dir, db, "real-disc", tmp_path / "backup", {}
    )
    assert result.get("applied") is True, f"writer did not apply: {result}"

    written = (session_dir / "real-disc.json").read_text()
    assert "data:image" not in written, f"role={role}: discoverability row was NOT bounded"
    assert "hi" in written


@pytest.mark.parametrize("label,parts", [
    ("image-only", [_huge_part(4000)]),
    ("text-then-image", [{"type": "text", "text": "hi"}, _huge_part(4000)]),
    ("image-then-image", [_huge_part(4000), _huge_part(4000)]),
])
def test_projected_content_stays_renderable(label, parts):
    """A bounded row must still decode back into supported parts.

    The reviewer noted image-only rows need "their own preservation-safe
    treatment; the existing display decoder's renderability filter is not a
    universal repair". This pins the actual property that matters: whatever the
    projector writes must pass `_decode_state_db_content` and remain a SUPPORTED
    part list, i.e. the row is still renderable after being bounded.
    """
    from api.models import _decode_state_db_content, _is_supported_content_part_list
    from api.session_image_persistence_override import project_message_payloads

    stored = "\x00json:" + json.dumps(parts)
    env = {"messages": [{"role": "user", "content": stored, "id": "a", "timestamp": 1.0}]}
    out = project_message_payloads(env)

    assert "data:image" not in json.dumps(out), f"{label}: not bounded"
    projected = out["messages"][0]["content"]
    decoded = _decode_state_db_content(projected)
    assert decoded is not projected, f"{label}: projected content no longer decodes"
    assert _is_supported_content_part_list(decoded), f"{label}: decoded parts unsupported"
    assert isinstance(decoded, list) and decoded, f"{label}: became an empty row"


def test_save_bounds_anchor_scene_snippets_end_to_end(tmp_path, monkeypatch):
    """`Session.save()` itself must bound anchor-scene snippets.

    The unit test for `compact_persisted_anchor_scenes` passes even if nothing
    CALLS it, so it cannot catch the actual defect (an unwired writer lane -- the
    same class of gap Greptile found twice). This drives the real save path and
    asserts on the bytes that land on disk.
    """
    import api.models as models

    sessions = tmp_path / "sessions"
    sessions.mkdir()
    monkeypatch.setattr(models, "SESSION_DIR", sessions)
    monkeypatch.setattr(models, "SESSION_INDEX_FILE", sessions / "_index.json")

    # A real scene snippet is a plain STRING holding a data URL (the captured
    # tool preview), not a structured part dict.
    payload = base64.b64encode(b"Z" * 4000).decode("ascii")
    snippet = f"{{'type': 'image_url', 'url': 'data:image/jpeg;base64,{payload}'}}"
    session = models.Session(session_id="anchor-e2e")
    session.anchor_activity_scenes = {
        "e" * 64: {
            "scene": {"activity_rows": [{"tool": {"snippet": snippet}, "payload": {"snippet": snippet}}]},
            "message_ref": "f" * 64,
            "message_index": 3,
            "stream_id": "st-1",
            "updated_at": 42.0,
        }
    }

    session.save()

    written = json.loads((sessions / "anchor-e2e.json").read_text(encoding="utf-8"))
    scenes = written.get("anchor_activity_scenes") or {}
    assert scenes, "anchor_activity_scenes was dropped entirely"
    assert "data:image" not in json.dumps(scenes), "save() persisted anchor-scene image bytes"
    assert payload not in json.dumps(written), "base64 survived the save"
    # Identity must survive so ref/index matching and the #5854 fingerprint agree.
    record = scenes["e" * 64]
    assert record["message_ref"] == "f" * 64
    assert record["message_index"] == 3
    assert record["stream_id"] == "st-1"
    assert record["updated_at"] == 42.0


def test_project_message_payloads_bounds_anchor_scene_snippets():
    """The SHARED writer entry point must bound anchor scenes too.

    `project_message_payloads` is what the recovery, discoverability,
    workspace-binding, shrink-backup and migration writers all call. It only
    projected `messages`/`context_messages`, so every one of those writers left
    anchor-scene snippet base64 in place -- which is exactly why 122 KB survived
    the migration across four sessions.
    """
    from api.session_image_persistence_override import project_message_payloads

    payload = base64.b64encode(b"Y" * 4000).decode("ascii")
    snippet = f"{{'url': 'data:image/jpeg;base64,{payload}'}}"
    envelope = {
        "messages": [{"role": "user", "content": "hi", "id": "m1", "timestamp": 1.0}],
        "anchor_activity_scenes": {
            "9" * 64: {
                "scene": {"activity_rows": [{"tool": {"snippet": snippet}}]},
                "message_ref": "8" * 64,
                "message_index": 2,
                "updated_at": 5.0,
            }
        },
    }

    projected = project_message_payloads(envelope)

    assert projected is not envelope, "anchor-scene base64 was not projected"
    assert payload not in json.dumps(projected), "base64 survived the shared projector"
    # The live payload must be untouched (copy-on-write contract).
    assert payload in json.dumps(envelope)
    record = projected["anchor_activity_scenes"]["9" * 64]
    assert record["message_ref"] == "8" * 64
    assert record["message_index"] == 2
    assert record["updated_at"] == 5.0


def test_clean_anchor_scene_payload_is_returned_unchanged():
    """No inline bytes anywhere -> the SAME object, so a clean write stays byte-identical."""
    from api.session_image_persistence_override import project_message_payloads

    envelope = {
        "messages": [{"role": "user", "content": "hi"}],
        "anchor_activity_scenes": {"k" * 64: {"scene": {"activity_rows": [{"tool": {"snippet": "text"}}]}}},
    }
    assert project_message_payloads(envelope) is envelope


def test_every_known_heavyweight_field_is_actually_bounded():
    """Behavioural guard: each field known to carry base64 must be bounded.

    `project_message_payloads` is key-driven, so a NEW top-level field carrying
    inline bytes would pass straight through -- and there is no safe blanket fix,
    because walking every string would rewrite provider payloads and compression
    state whose byte-exactness is contractual.

    These are the fields that carry >=256-char base64 in the live store today,
    measured by enumerating all 108 session files. This asserts the PROJECTOR's
    actual behaviour on each, so dropping one of the lanes fails here rather than
    silently reopening the bug class. Adding a heavyweight field to the session
    schema means adding it here on purpose.
    """
    from api.session_image_persistence_override import project_message_payloads

    payload = base64.b64encode(b"K" * 4000).decode("ascii")
    url = f"data:image/jpeg;base64,{payload}"
    part = {"type": "image_url", "image_url": {"url": url}}
    stamp = {"id": "m0", "timestamp": 1.0}

    cases = {
        # field -> an envelope whose ONLY inline bytes live in that field
        "messages": {"messages": [{"role": "user", "content": [part], **stamp}]},
        "api_content": {"messages": [{"role": "user", "content": "hi", "api_content": json.dumps([part]), **stamp}]},
        "_partial_tool_calls": {"messages": [{"role": "user", "content": "hi", "_partial_tool_calls": [{"args": {"p": url}}], **stamp}]},
        "anchor_activity_scenes": {
            "anchor_activity_scenes": {
                "a" * 64: {"scene": {"activity_rows": [{"tool": {"snippet": f"{{'u': '{url}'}}"}}]}}
            }
        },
        "content as \\x00json: string": {"messages": [{"role": "user", "content": "\x00json:" + json.dumps([part]), **stamp}]},
        "content as plain string": {"messages": [{"role": "user", "content": url, **stamp}]},
    }

    for label, envelope in cases.items():
        projected = project_message_payloads(envelope)
        assert payload not in json.dumps(projected), f"{label}: inline bytes survived projection"
        # context_messages is never projected; assert that too so the exemption is
        # pinned rather than assumed.
        ctx = {"context_messages": [{"role": "user", "content": [part], **stamp}]}
        assert project_message_payloads(ctx) is ctx, "context_messages must stay byte-exact"
