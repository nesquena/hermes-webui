"""Structural `done_at` field: sidecar round-trip + model contract.

`done_at` is the structural "this conversation is finished" flag, replacing the
inference from a title prefix + silence. Two things must hold or the feature
silently no-ops:

1. the field survives a write through `WebUIJsonSessionDB.update_metadata()`
   and a read back through `list_sessions()` (it is a whitelisted metadata
   field — drop it from `_METADATA_FIELDS` and it is discarded without error);
2. `Session` carries it through save() / load() / compact(), so the sidebar
   row and a full reload agree.

`done_at` must stay distinct from `archived`: archived is storage, done is
workflow, and both can be true at once.
"""

import json

import pytest

import api.models as models
from api.webui_session_db import WebUIJsonSessionDB


@pytest.fixture
def session_dir(tmp_path, monkeypatch):
    path = tmp_path / "sessions"
    path.mkdir()
    monkeypatch.setattr(models, "SESSION_DIR", path)
    return path


def _write_json_session(session_dir, sid="session_done", **overrides):
    payload = {
        "session_id": sid,
        "title": "Done Field Session",
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
        "tool_calls": [],
    }
    payload.update(overrides)
    payload["message_count"] = len(payload["messages"])
    path = session_dir / f"{sid}.json"
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    return payload, path


def test_done_at_is_a_whitelisted_metadata_field():
    from api.webui_session_db import _METADATA_FIELDS

    assert "done_at" in _METADATA_FIELDS, (
        "without the whitelist entry update_metadata() drops done_at silently"
    )


def test_update_metadata_round_trips_done_at(session_dir):
    _, path = _write_json_session(session_dir)

    row = WebUIJsonSessionDB().update_metadata("session_done", {"done_at": 1234.5})
    assert row["done_at"] == 1234.5

    on_disk = json.loads(path.read_text(encoding="utf-8"))
    assert on_disk["done_at"] == 1234.5

    listed = {r["session_id"]: r for r in WebUIJsonSessionDB().list_sessions()}
    assert listed["session_done"]["done_at"] == 1234.5


def test_update_metadata_can_clear_done_at(session_dir):
    _write_json_session(session_dir, done_at=99.0)
    row = WebUIJsonSessionDB().update_metadata("session_done", {"done_at": None})
    assert row["done_at"] is None


def test_done_at_does_not_imply_archived(session_dir):
    _, path = _write_json_session(session_dir)
    WebUIJsonSessionDB().update_metadata("session_done", {"done_at": 5.0})
    on_disk = json.loads(path.read_text(encoding="utf-8"))
    assert on_disk["done_at"] == 5.0
    assert on_disk["archived"] is False


def test_session_save_load_preserves_done_at(session_dir):
    session = models.Session(session_id="session_done", title="T", done_at=4242.0)
    session.save()

    full = models.Session.load("session_done")
    assert full.done_at == 4242.0

    meta = models.Session.load_metadata_only("session_done")
    assert meta.done_at == 4242.0


def test_session_compact_exposes_done_at(session_dir):
    session = models.Session(session_id="session_done", title="T", done_at=7.0)
    assert session.compact()["done_at"] == 7.0
    assert session.compact()["archived"] is False


def test_session_tolerates_legacy_sidecar_without_done_at(session_dir):
    _write_json_session(session_dir)
    session = models.Session.load("session_done")
    assert session.done_at is None


def test_session_ignores_malformed_done_at(session_dir):
    _write_json_session(session_dir, done_at="not-a-timestamp")
    session = models.Session.load("session_done")
    assert session.done_at is None


def test_a_stale_snapshot_writer_cannot_revert_done_at(session_dir):
    """The CLI-import refresh loads its own Session before reading CLI history
    and saves it afterwards; a mark-done landing in that window must survive.

    The refresh is modelled verbatim: two independent objects for one id, both
    loaded before ANY save, the endpoint's copy writing `done_at` first.
    """
    models.Session(session_id="race", title="T").save()
    stale_refresh = models.Session.load("race")  # the concurrent writer's copy
    endpoint = models.Session.load("race")       # the mark-done copy

    endpoint.done_at = 777.0
    endpoint.save(touch_updated_at=False)

    stale_refresh.title = "refreshed from CLI"
    stale_refresh.save(touch_updated_at=False)

    assert models.Session.load("race").done_at == 777.0
    # The stale writer adopted the durable value instead of keeping its own.
    assert stale_refresh.done_at == 777.0


def test_a_stale_snapshot_writer_cannot_restore_a_cleared_done_at(session_dir):
    """Same class, other direction: an un-mark must not be undone."""
    models.Session(session_id="race2", title="T", done_at=1234.0).save()
    stale_refresh = models.Session.load("race2")
    endpoint = models.Session.load("race2")

    endpoint.done_at = None
    endpoint.save(touch_updated_at=False)

    stale_refresh.title = "refreshed from CLI"
    stale_refresh.save(touch_updated_at=False)

    assert models.Session.load("race2").done_at is None


def test_an_own_done_at_change_still_wins_over_the_sidecar(session_dir):
    """The guard must not freeze the field: the writer that CHANGED it wins."""
    models.Session(session_id="own", title="T", done_at=5.0).save()
    session = models.Session.load("own")
    session.done_at = 6.0
    session.save(touch_updated_at=False)
    assert models.Session.load("own").done_at == 6.0


@pytest.mark.parametrize("stored", ["nan", "inf", "-inf", "Infinity", -5, "1e400"])
def test_non_finite_done_at_never_reaches_a_read_path(session_dir, stored):
    """A hand-edited sidecar must not emit a bare NaN/Infinity JSON token.

    Every read path is checked: load, load_metadata_only, compact and the
    metadata projection in WebUIJsonSessionDB (which parses raw JSON and never
    builds a Session). `allow_nan=False` is the browser's contract.
    """
    _write_json_session(session_dir, done_at=stored)

    full = models.Session.load("session_done")
    assert full.done_at is None
    assert models.Session.load_metadata_only("session_done").done_at is None

    compact = full.compact()
    assert compact["done_at"] is None
    json.dumps(compact, allow_nan=False)

    row = {r["session_id"]: r for r in WebUIJsonSessionDB().list_sessions()}["session_done"]
    assert row["done_at"] is None
    json.dumps(row, allow_nan=False)


def test_save_writes_null_not_a_bare_nan_token(session_dir):
    """A direct `session.done_at = float('nan')` (bypassing __init__) must not
    reach the sidecar: json.dumps would write the `NaN` token JSON.parse
    rejects, breaking the sidebar for every client until the file is repaired."""
    session = models.Session(session_id="nanwrite", title="T")
    session.done_at = float("nan")
    session.save()

    raw = (session_dir / "nanwrite.json").read_text(encoding="utf-8")
    assert "NaN" not in raw
    assert '"done_at": null' in raw
    assert models.Session.load("nanwrite").done_at is None


def test_update_metadata_cannot_store_a_non_finite_done_at(session_dir):
    """The dormant adapter's raw write path normalizes too."""
    _, path = _write_json_session(session_dir)
    WebUIJsonSessionDB().update_metadata("session_done", {"done_at": float("inf")})
    raw = path.read_text(encoding="utf-8")
    assert "Infinity" not in raw
    assert json.loads(raw)["done_at"] is None
