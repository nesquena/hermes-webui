"""Coverage for the metadata-head read used by ``WebUIJsonSessionDB.list_sessions()``.

``list_sessions()`` only needs the scalar metadata of each sidecar, but it used to
``json.loads`` the whole file -- 13.8-17.8 s per refresh over the live store (637
files / 1.18 GB), every byte of the transcripts included. The sidecar layout puts
all metadata before ``messages``, so the row source now parses the head only.

These tests pin the three properties that make that safe:
  * the metadata prefix decoded from the head is identical to the one a full
    parse yields (so the sidebar cannot drift from the full reader),
  * a sidecar without a usable ``message_count`` still reports a truthful count
    (falls back to a full parse instead of showing 0 messages),
  * ``messages`` is never part of a head-read result, and a prefix that overflows
    the window is served by the shared budgeted reader rather than silently
    dropping fields.

Timing is deliberately not asserted: it depends on the host's page cache.
"""
import json
from pathlib import Path

import pytest

import api.models as models
import api.webui_session_db as session_db

DB = session_db.WebUIJsonSessionDB

#: The six projected fields the original reproduction moves after ``messages``.
REORDERED_FIELDS = (
    "title",
    "profile",
    "archived",
    "pinned",
    "raw_source",
    "source_label",
)


@pytest.fixture
def session_store(tmp_path, monkeypatch):
    sdir = tmp_path / "sessions"
    sdir.mkdir(parents=True, exist_ok=True)
    monkeypatch.setattr(models, "SESSION_DIR", sdir)
    return sdir


def _big_messages(count=400, text_size=2000):
    return [
        {"role": "user" if i % 2 == 0 else "assistant",
         "content": f"message {i} " + "X" * text_size,
         "timestamp": 1000.0 + i}
        for i in range(count)
    ]


def _save_modern_session(session_store, sid="s1", messages=None, title="Metadata Head"):
    """Write a sidecar through the real writer, so the key order is the contract."""
    session = models.Session(
        session_id=sid,
        title=title,
        workspace=str(session_store.parent),
        model="test-model",
        messages=messages if messages is not None else _big_messages(),
    )
    session.save()
    return session_store / f"{sid}.json"


def _full_row(path):
    data = DB._read_path(path)
    assert isinstance(data, dict)
    return DB._metadata_row(str(data.get("session_id") or path.stem), data)


def test_head_metadata_matches_full_parse_on_large_sidecar(session_store):
    path = _save_modern_session(session_store)
    assert path.stat().st_size > DB._METADATA_HEAD_BYTES * 4

    head = DB._read_metadata_head(path)
    assert isinstance(head, dict)
    assert "messages" not in head

    source = DB._read_metadata_row_source(path)
    assert isinstance(source, dict)
    assert "messages" not in source

    full = DB._read_path(path)
    assert isinstance(full, dict)
    sid = str(full["session_id"])
    assert DB._metadata_row(sid, source) == DB._metadata_row(sid, full)

    row = DB(session_store).list_sessions()[0]
    assert row == DB._metadata_row(sid, full)
    assert row["message_count"] == 400
    assert row["title"] == "Metadata Head"


def test_head_metadata_covers_every_adapter_field(session_store):
    path = _save_modern_session(session_store, sid="s2")
    source = DB._read_metadata_row_source(path)
    full = DB._read_path(path)
    assert isinstance(source, dict) and isinstance(full, dict)

    expected_fields = (session_db._METADATA_FIELDS | {"session_id", "message_count"}) & set(full)
    for field in expected_fields:
        assert source.get(field) == full.get(field), field


def test_missing_message_count_falls_back_to_full_parse(session_store):
    """A sidecar without ``message_count`` must not render as 0 messages."""
    payload = {
        "session_id": "legacy",
        "title": "Legacy",
        "created_at": 100.0,
        "updated_at": 200.0,
        "messages": [{"role": "user", "content": "hello " + "X" * 500}, {"role": "user", "content": "again"}],
    }
    path = session_store / "legacy.json"
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")

    # The head read alone cannot answer: the count is derived from `messages`.
    # A legacy sidecar carries none of the current writer's layout keys, so the
    # cut is refused and the row comes from the full read.
    assert DB._read_metadata_head(path) is None

    source = DB._read_metadata_row_source(path)
    assert isinstance(source, dict)
    row = DB._metadata_row("legacy", source)
    assert row["message_count"] == 2
    assert DB(session_store).list_sessions()[0]["message_count"] == 2
    assert source["title"] == "Legacy"


def test_oversized_metadata_prefix_uses_budgeted_reader(session_store, monkeypatch):
    """Metadata past the head window is still read without a full parse."""
    payload = {
        "session_id": "fat",
        "title": "Fat Prefix",
        "workspace": "/tmp/fat",
        "model": "test-model",
        "created_at": 100.0,
        "updated_at": 200.0,
        "last_message_at": 200.0,
        "compression_anchor_summary": "S" * (DB._METADATA_HEAD_BYTES + 16 * 1024),
        "message_count": 3,
        "_mc_v": models._MESSAGE_COUNT_MARKER,
        "_row_layout": models._ROW_LAYOUT_VERSION,
        "anchor_scene_index": {},
        "archived": False,
        "profile": "default",
        "pinned": False,
        "raw_source": "webui",
        "source_label": "WebUI",
        "messages": [{"role": "user", "content": "hi"}] * 3,
    }
    path = session_store / "fat.json"
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")

    assert DB._read_metadata_head(path) is None

    def _no_full_parse(_path):
        raise AssertionError("oversized prefix should be served by the budgeted reader")

    monkeypatch.setattr(DB, "_read_path", staticmethod(_no_full_parse))
    source = DB._read_metadata_row_source(path)
    assert isinstance(source, dict)
    assert "messages" not in source
    assert source["session_id"] == "fat"
    assert source["message_count"] == 3


def test_compact_sidecar_of_the_writer_contract_is_read_from_the_head(session_store):
    """Key layout, not whitespace, decides: a marked compact head still cuts.

    The head must physically show every projected key ahead of the cut, so this
    payload carries the fields ``Session.save`` writes ahead of ``messages``. The
    compact separators (no indentation) are the only difference from the real
    layout: the cut is found by key position, not by the ``\\n  "messages":``
    indented form.
    """
    session = models.Session(
        session_id="compact",
        title="Compact",
        workspace=str(session_store.parent),
        model="test-model",
        messages=[{"role": "user", "content": "a" * 400}, {"role": "assistant", "content": "b"}],
    )
    payload = {
        "session_id": "compact",
        "title": "Compact",
        "workspace": str(session_store.parent),
        "model": "test-model",
        "created_at": 100.0,
        "updated_at": 200.0,
        "last_message_at": 200.0,
        "message_count": 2,
        "_mc_v": models._MESSAGE_COUNT_MARKER,
        "_row_layout": models._ROW_LAYOUT_VERSION,
        "anchor_scene_index": {},
        "archived": False,
        "profile": "default",
        "pinned": False,
        "raw_source": "webui",
        "source_label": "WebUI",
        "messages": session.messages,
    }
    path = session_store / "compact.json"
    path.write_text(json.dumps(payload, separators=(",", ":")), encoding="utf-8")

    source = DB._read_metadata_row_source(path)
    assert isinstance(source, dict)
    assert "messages" not in source
    assert source["title"] == "Compact"
    assert DB._metadata_row("compact", source)["message_count"] == 2


def test_marker_without_the_projected_keys_is_not_admitted(session_store):
    """A marker alone must not admit a cut: the head has to show the keys.

    ``write_session()`` can preserve a ``_row_layout`` it did not mint, and a
    caller can serialize title/profile/archived after ``messages``. Admission
    therefore requires the marker AND every projected key ahead of the cut.
    """
    payload = {
        "session_id": "marker-only",
        "created_at": 100.0,
        "updated_at": 200.0,
        "message_count": 2,
        "_mc_v": models._MESSAGE_COUNT_MARKER,
        "_row_layout": models._ROW_LAYOUT_VERSION,
        "messages": [{"role": "user", "content": "one"}, {"role": "assistant", "content": "two"}],
        "title": "Marker Only",
        "profile": "work",
        "archived": True,
        "pinned": False,
        "raw_source": "telegram",
        "source_label": "Telegram",
    }
    path = session_store / "marker-only.json"
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")

    assert DB._read_metadata_head(path) is None
    source = DB._read_metadata_row_source(path)
    assert isinstance(source, dict)
    assert source["title"] == "Marker Only"
    assert source["profile"] == "work"
    assert source["archived"] is True
    assert source["raw_source"] == "telegram"


def test_messages_before_metadata_takes_the_full_read(session_store):
    """A payload ordered messages-first must not lose title/profile/archived.

    ``write_session()`` preserves the caller's dictionary order, so a valid
    payload can serialize ``messages`` before the rest of the metadata. Cutting
    at that key would return a row missing every later field. The layout check
    refuses the cut even when the count marker is present.
    """
    payload = {
        "session_id": "reordered",
        "message_count": 1,
        "_mc_v": models._MESSAGE_COUNT_MARKER,
        "messages": [{"role": "user", "content": "hi"}],
        "title": "Reordered Title",
        "profile": "work",
        "archived": True,
        "created_at": 100.0,
        "updated_at": 200.0,
    }
    path = session_store / "reordered.json"
    path.write_text(json.dumps(payload, indent=2), encoding="utf-8")

    assert DB._read_metadata_head(path) is None

    source = DB._read_metadata_row_source(path)
    full = DB._read_path(path)
    assert isinstance(source, dict) and isinstance(full, dict)
    sid = str(full["session_id"])
    assert DB._metadata_row(sid, source) == DB._metadata_row(sid, full)

    row = DB(session_store).list_sessions()[0]
    assert row["title"] == "Reordered Title"
    assert row["profile"] == "work"
    assert row["archived"] is True
    assert row["message_count"] == 1


def test_write_session_round_trip_keeps_metadata_after_messages(session_store):
    """The round trip through the adapter's own writer keeps every row field."""
    db = DB(session_store)
    payload = {
        "session_id": "rt",
        "message_count": 2,
        "messages": [{"role": "user", "content": "one"}, {"role": "assistant", "content": "two"}],
        "title": "Round Trip",
        "profile": "work",
        "archived": True,
        "pinned": True,
    }
    db.write_session(payload)
    path = session_store / "rt.json"

    source = db._read_metadata_row_source(path)
    full = db._read_path(path)
    assert isinstance(source, dict) and isinstance(full, dict)
    assert db._metadata_row("rt", source) == db._metadata_row("rt", full)

    row = db.list_sessions()[0]
    assert row["title"] == "Round Trip"
    assert row["profile"] == "work"
    assert row["archived"] is True
    assert row["pinned"] is True
    assert row["message_count"] == 2


def test_write_session_normalizes_projected_keys_ahead_of_messages(session_store):
    """The generic writer now emits the ordering it retires the marker for.

    Retiring ``_row_layout`` keeps the row correct, but it also means a sidecar
    this writer produced can only be served from the head once the projected
    keys really precede ``messages``. They do: the writer re-emits them ahead of
    the transcript, so the full read and the head agree.
    """
    db = DB(session_store)
    messages = [{"role": "user", "content": "one"}, {"role": "assistant", "content": "two"}]
    payload = {
        "session_id": "ordered",
        "messages": messages,
        "title": "Ordered",
        "profile": "work",
        "archived": True,
        "pinned": True,
        "raw_source": "telegram",
        "source_label": "Telegram",
        "created_at": 100.0,
        "updated_at": 200.0,
        "message_count": 1,
        "_mc_v": models._MESSAGE_COUNT_MARKER,
    }
    written = db.write_session(payload)

    keys = list(written)
    assert keys.index("messages") > max(keys.index(field) for field in (
        "session_id", "title", "created_at", "updated_at", "message_count",
        "profile", "archived", "pinned", "raw_source", "source_label",
    ))
    assert written["message_count"] == 2
    assert written["messages"] == messages
    # The count marker keeps its own positional promise: bounded prefix readers
    # (``_prefix_message_count``) must still find ``_mc_v`` ahead of messages.
    if "_mc_v" in written:
        assert keys.index("_mc_v") < keys.index("messages")
        assert models._prefix_message_count(session_store / "ordered.json") == 2

    # A hand-written marker on a payload this writer reordered is retired, so no
    # prefix can be admitted on a promise the writer did not make.
    db.write_session({**written, "_row_layout": models._ROW_LAYOUT_VERSION})
    stored = json.loads((session_store / "ordered.json").read_text(encoding="utf-8"))
    assert "_row_layout" not in stored

    row = db.list_sessions()[0]
    full = db._read_path(session_store / "ordered.json")
    assert isinstance(full, dict)
    assert db._metadata_row("ordered", stored) == db._metadata_row("ordered", full)
    assert row["title"] == "Ordered"
    assert row["raw_source"] == "telegram"
    assert row["message_count"] == 2


def test_ordinary_writer_fast_path_never_full_parses(session_store, monkeypatch):
    """The ordinary ``Session.save()`` sidecar still serves rows from the head."""
    _save_modern_session(
        session_store, sid="fast", messages=[{"role": "user", "content": "hi"}]
    )

    def _no_full_parse(_path):
        raise AssertionError("a marked writer layout must be served from the head")

    monkeypatch.setattr(DB, "_read_path", staticmethod(_no_full_parse))
    row = DB(session_store).list_sessions()[0]
    assert row["title"] == "Metadata Head"
    assert row["message_count"] == 1


def test_update_metadata_retires_a_marker_it_cannot_vouch_for(session_store):
    """The in-place metadata writer must not preserve a layout promise.

    ``update_metadata`` rewrites the file in its existing key order and can
    append a projected field it did not have, so it cannot vouch that every
    projected key precedes ``messages``. A marker inherited from the input is
    retired -- the same rule as ``write_session`` -- and the row stays complete
    because it comes from the full read.
    """
    payload = {
        "session_id": "upd",
        "message_count": 2,
        "_mc_v": models._MESSAGE_COUNT_MARKER,
        "_row_layout": models._ROW_LAYOUT_VERSION,
        "messages": [{"role": "user", "content": "one"}, {"role": "assistant", "content": "two"}],
        "title": "Before",
        "profile": "work",
        "archived": True,
        "pinned": True,
        "raw_source": "telegram",
        "source_label": "Telegram",
        "created_at": 100.0,
        "updated_at": 200.0,
    }
    path = session_store / "upd.json"
    path.write_text(json.dumps(payload, indent=2), encoding="utf-8")

    db = DB(session_store)
    db.update_metadata("upd", {"title": "Updated"})

    stored = json.loads(path.read_text(encoding="utf-8"))
    assert stored["title"] == "Updated"
    assert "_row_layout" not in stored

    row = db.list_sessions()[0]
    assert row["title"] == "Updated"
    assert row["profile"] == "work"
    assert row["archived"] is True
    assert row["pinned"] is True
    assert row["raw_source"] == "telegram"
    assert row["source_label"] == "Telegram"
    assert row["message_count"] == 2


def _reorder_real_writer_payload(session_store, sid, *, keep_anchor_scene_index):
    """Persist a real writer payload, then replay it through ``write_session()``
    with only the six projected fields moved AFTER ``messages``.

    Mirrors the original oracle literally: step 1 reads the complete payload the
    real writer produced through the public ``read_session()``; step 2 moves
    **only** ``title``/``profile``/``archived``/``pinned``/``raw_source``/
    ``source_label`` after ``messages`` and retains every other actual key in
    its original position -- including ``_row_layout``, ``_mc_v``,
    ``message_count``, the timestamps, ``tool_calls`` and the anchor bodies.
    Dropping the marker while building the input would test a payload the
    reproduction never used, and would hide the case where a generic writer
    preserves a marker it did not mint. Returns the sidecar path and the
    payload the writer actually produced.
    """
    session = models.Session(
        session_id=sid,
        title="Reordered Real",
        workspace=str(session_store.parent),
        model="test-model",
        profile="work",
        pinned=True,
        archived=True,
        raw_source="telegram",
        source_label="Telegram",
        messages=[{"role": "user", "content": "one"}, {"role": "assistant", "content": "two"}],
    )
    session.save()
    db = DB(session_store)
    real = db.read_session(sid)
    assert isinstance(real, dict)
    assert real["_row_layout"] == models._ROW_LAYOUT_VERSION

    # Carry every actual key of the writer payload in its writer position, then
    # append only the six projected fields after ``messages``.
    reordered = {
        key: value
        for key, value in real.items()
        if key != "messages" and key not in REORDERED_FIELDS
    }
    if not keep_anchor_scene_index:
        reordered.pop("anchor_scene_index", None)
    reordered["messages"] = real["messages"]
    for field in REORDERED_FIELDS:
        reordered[field] = real[field]
    written = db.write_session(reordered)
    return session_store / f"{sid}.json", real, written


@pytest.mark.parametrize("keep_anchor_scene_index", [True, False], ids=["anchor-retained", "anchor-removed"])
def test_reordered_real_payload_keeps_every_projected_field(session_store, keep_anchor_scene_index):
    """Both admissions must refuse a row that lacks the complete-row guarantee.

    A retained ``anchor_scene_index`` used to admit the head cut; without it the
    ``_ROW_FIELDS`` subset admitted the budgeted fallback. Both dropped the six
    projected fields moved after ``messages``. Neither prefix carries the writer's
    ``_row_layout``, so both must fall back to the full read.
    """
    sid = "reordered-real"
    path, real, written = _reorder_real_writer_payload(
        session_store, sid, keep_anchor_scene_index=keep_anchor_scene_index
    )

    # Row correctness first: on a head/budgeted admission that trusts the
    # surviving ``_row_layout``, this is where the six projected fields go
    # missing.
    db = DB(session_store)
    source = db._read_metadata_row_source(path)
    assert source is not None
    full = db._read_path(path)
    assert isinstance(full, dict)
    assert db._metadata_row(sid, source) == db._metadata_row(sid, full)

    row = db.list_sessions()[0]
    assert row["title"] == "Reordered Real"
    assert row["profile"] == "work"
    assert row["archived"] is True
    assert row["pinned"] is True
    assert row["raw_source"] == "telegram"
    assert row["source_label"] == "Telegram"
    assert row["message_count"] == 2
    assert row["created_at"] == real["created_at"]
    assert row["updated_at"] == real["updated_at"]

    # Neither admission may serve a prefix for this layout: the head declines,
    # and ``write_session`` preserved the caller's order verbatim, so it cannot
    # vouch for the marker and must retire it rather than pass it through.
    assert DB._read_metadata_head(path) is None
    assert "_row_layout" not in written
    assert "_row_layout" not in json.loads(path.read_text(encoding="utf-8"))


def test_budgeted_prefix_without_layout_marker_takes_the_full_read(session_store, monkeypatch):
    """``_ROW_FIELDS`` + a count are not proof: the fallback needs the marker too."""
    payload = {
        "session_id": "subset",
        "message_count": 1,
        "_mc_v": models._MESSAGE_COUNT_MARKER,
        "created_at": 100.0,
        "updated_at": 200.0,
        "messages": [{"role": "user", "content": "hi"}],
        "title": "After Messages",
        "profile": "work",
    }
    path = session_store / "subset.json"
    path.write_text(json.dumps(payload, indent=2), encoding="utf-8")

    monkeypatch.setattr(DB, "_read_metadata_head", classmethod(lambda cls, p: None))
    source = DB._read_metadata_row_source(path)
    full = DB._read_path(path)
    assert isinstance(source, dict) and isinstance(full, dict)
    assert source.get("title") == "After Messages"
    assert DB._metadata_row("subset", source) == DB._metadata_row("subset", full)


def test_attached_last_message_at_is_written_ahead_of_messages(session_store):
    """The head cut must expose an explicitly attached ordering value.

    Re-gate fixture: ``updated_at=200`` with an attached ``last_message_at=100``.
    The key used to fall through to ``extra`` and serialize AFTER ``messages``,
    so the head-prefix reader could not see it and fell back to ``updated_at`` —
    listing 200 where the full read listed 100.
    """
    sid = "attached-lma"
    session = models.Session(
        session_id=sid,
        title="Attached LMA",
        workspace=str(session_store.parent),
        model="test-model",
        messages=[{"role": "user", "content": "hi", "timestamp": 50.0}],
        updated_at=200.0,
    )
    session.last_message_at = 100.0
    session.save(touch_updated_at=False)

    path = session_store / f"{sid}.json"
    raw = path.read_text(encoding="utf-8")
    assert raw.index('"last_message_at"') < raw.index('"messages"'), (
        "the attached ordering value must sit in the metadata head, not after "
        "the transcript the head never parses"
    )

    full = DB._read_path(path)
    assert full["last_message_at"] == 100.0
    row = DB(session_store).list_sessions()[0]
    assert row["last_message_at"] == 100.0, (
        "the head-derived row must agree with the full read on the ordering value"
    )


def test_oversized_reordered_prefix_takes_the_full_read(session_store, monkeypatch):
    """An oversized prefix without a valid layout proof must fall back whole.

    ``test_oversized_metadata_prefix_uses_budgeted_reader`` covers the admitted
    oversized case, where the projected fields already precede ``messages``.
    This is the same 80,000-byte metadata overflow reached by the original
    reproduction: the real writer payload is read through public
    ``read_session()``, only the six projected fields move after the transcript,
    and it is persisted through public ``write_session()``. The head really
    declines on size, and the budgeted reader really admits the oversized
    prefix -- but the generic writer retired ``_row_layout``, so that admission
    must decline too and the row must come from a full read with every projected
    field intact.
    """
    sid = "fat-reordered"
    session = models.Session(
        session_id=sid,
        title="Fat Reordered",
        workspace=str(session_store.parent),
        model="test-model",
        profile="work",
        pinned=True,
        archived=True,
        raw_source="telegram",
        source_label="Telegram",
        messages=[{"role": "user", "content": "hi"}],
    )
    session.save()
    db = DB(session_store)
    real = db.read_session(sid)
    assert isinstance(real, dict)

    reordered = {
        key: value
        for key, value in real.items()
        if key != "messages" and key not in REORDERED_FIELDS
    }
    # 80,000 bytes of metadata: past the 64 KiB head window, inside the 1 MiB
    # budgeted window -- the actual oversized shared-prefix admission.
    reordered["compression_anchor_summary"] = "S" * 80_000
    reordered["messages"] = real["messages"]
    for field in REORDERED_FIELDS:
        reordered[field] = real[field]

    written = db.write_session(reordered)
    path = session_store / f"{sid}.json"
    assert DB._read_metadata_head(path) is None
    # The generic writer preserves the caller's other key order, so it cannot
    # vouch for this layout: the marker is retired, and no prefix may be served.
    assert "_row_layout" not in written

    # Prove the budgeted reader was really reached and really admitted a prefix
    # (not that the head quietly served the row, and not that the helper that
    # opens the file failed): the shape it gets carries the count marker this
    # writer did relocate, but no layout marker.
    seen: list[str] = []
    real_budgeted = models._read_metadata_json_prefix

    def _spy(p, *args, **kwargs):
        raw = real_budgeted(p, *args, **kwargs)
        seen.append(raw or "")
        return raw

    monkeypatch.setattr(models, "_read_metadata_json_prefix", _spy)
    source = db._read_metadata_row_source(path)
    monkeypatch.setattr(models, "_read_metadata_json_prefix", real_budgeted)

    assert len(seen) == 1
    admitted_prefix = json.loads(seen[0])
    assert "_mc_v" in admitted_prefix
    assert "message_count" in admitted_prefix
    assert "_row_layout" not in admitted_prefix

    assert isinstance(source, dict)
    # No prefix was admitted: this is the complete document, so it carries the
    # transcript as well. That is the point -- order no longer matters once the
    # row comes from the full read.
    assert "messages" in source
    assert "_row_layout" not in source

    full = db._read_path(path)
    assert isinstance(full, dict)
    assert db._metadata_row(sid, source) == db._metadata_row(sid, full)

    row = db.list_sessions()[0]
    assert row["title"] == "Fat Reordered"
    assert row["profile"] == "work"
    assert row["archived"] is True
    assert row["pinned"] is True
    assert row["raw_source"] == "telegram"
    assert row["source_label"] == "Telegram"
    assert row["message_count"] == 1


def test_oversized_marked_prefix_missing_a_projected_key_takes_the_full_read(
    session_store, monkeypatch
):
    """The budgeted admission needs the projected keys, not just the marker.

    ``test_oversized_metadata_prefix_uses_budgeted_reader`` covers the admitted
    oversized case and ``test_marker_without_the_projected_keys_is_not_admitted``
    covers the head cut. The second admission has its own shape: past the 64 KiB
    head window the marker and the count are inside the 1 MiB budgeted prefix
    while title/profile/archived/pinned/raw_source/source_label sit after
    ``messages``. The marker alone admits that prefix, so the projected-key check
    is the only thing that keeps the six fields in the row.
    """
    payload = {
        "session_id": "fat-marker-only",
        "created_at": 100.0,
        "updated_at": 200.0,
        "message_count": 1,
        "_mc_v": models._MESSAGE_COUNT_MARKER,
        "_row_layout": models._ROW_LAYOUT_VERSION,
        "anchor_scene_index": {},
        "compression_anchor_summary": "S" * 80_000,
        "messages": [{"role": "user", "content": "hi"}],
        "title": "Marker Only Fat",
        "profile": "work",
        "archived": True,
        "pinned": True,
        "raw_source": "telegram",
        "source_label": "Telegram",
    }
    path = session_store / "fat-marker-only.json"
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")

    # The head really declines on size, so the budgeted reader is the admission
    # under test -- and the prefix it returns really carries the marker.
    assert DB._read_metadata_head(path) is None
    seen: list[str] = []
    real_budgeted = models._read_metadata_json_prefix

    def _spy(p, *args, **kwargs):
        raw = real_budgeted(p, *args, **kwargs)
        seen.append(raw or "")
        return raw

    monkeypatch.setattr(models, "_read_metadata_json_prefix", _spy)
    source = DB._read_metadata_row_source(path)
    monkeypatch.setattr(models, "_read_metadata_json_prefix", real_budgeted)

    assert len(seen) == 1
    prefix = json.loads(seen[0])
    assert prefix["_row_layout"] == models._ROW_LAYOUT_VERSION
    assert "message_count" in prefix and "messages" not in prefix
    assert "title" not in prefix, "the six projected fields must be past the cut"

    # No prefix was admitted: the row comes from the complete document.
    assert isinstance(source, dict)
    assert "messages" in source
    full = DB._read_path(path)
    assert isinstance(full, dict)
    assert DB._metadata_row("fat-marker-only", source) == DB._metadata_row("fat-marker-only", full)

    row = DB(session_store).list_sessions()[0]
    assert row["title"] == "Marker Only Fat"
    assert row["profile"] == "work"
    assert row["archived"] is True
    assert row["pinned"] is True
    assert row["raw_source"] == "telegram"
    assert row["source_label"] == "Telegram"
    assert row["message_count"] == 1


def test_recovery_writer_neither_mints_nor_invalidates_the_row_layout(
    session_store, monkeypatch
):
    """Audit of the other marker-carrying writer: recovery preserves positions.

    ``api.session_recovery._with_marked_message_count`` re-emits the caller's
    keys in place and only inserts ``message_count``/``_mc_v`` ahead of the
    transcript, so it can neither mint a layout promise nor move a projected
    field past the cut. A marked payload therefore keeps a promise that is
    still true, and an unmarked one still earns no marker.
    """
    from api.session_recovery import _with_marked_message_count

    db = DB(session_store)
    messages = [{"role": "user", "content": "one"}, {"role": "assistant", "content": "two"}]
    marked = {
        "session_id": "rec-marked",
        "created_at": 100.0,
        "updated_at": 200.0,
        "last_message_at": 200.0,
        "message_count": 2,
        "_mc_v": models._MESSAGE_COUNT_MARKER,
        "_row_layout": models._ROW_LAYOUT_VERSION,
        "anchor_scene_index": {},
        "title": "Recovery Marked",
        "profile": "work",
        "archived": True,
        "pinned": True,
        "raw_source": "telegram",
        "source_label": "Telegram",
        "messages": messages,
    }
    written = _with_marked_message_count(marked)
    keys = list(written)
    assert written["_row_layout"] == models._ROW_LAYOUT_VERSION
    assert keys.index("_row_layout") < keys.index("messages")
    assert keys.index("_mc_v") < keys.index("messages")
    for field in REORDERED_FIELDS:
        assert keys.index(field) < keys.index("messages"), field

    path = session_store / "rec-marked.json"
    path.write_text(json.dumps(written, ensure_ascii=False, indent=2), encoding="utf-8")
    # The promise survived, so the fast path still serves this row from the head.
    assert DB._read_metadata_head(path) is not None
    full = db._read_path(path)
    assert isinstance(full, dict)
    source = db._read_metadata_row_source(path)
    assert isinstance(source, dict) and "messages" not in source
    assert db._metadata_row("rec-marked", source) == db._metadata_row("rec-marked", full)
    row = db.list_sessions()[0]
    assert row["title"] == "Recovery Marked"
    assert row["raw_source"] == "telegram"
    assert row["message_count"] == 2

    # A layout it did not produce gains no marker, and the row still comes whole.
    reordered = {
        key: value
        for key, value in marked.items()
        if key != "messages" and key not in REORDERED_FIELDS and key != "_row_layout"
    }
    reordered["messages"] = messages
    for field in REORDERED_FIELDS:
        reordered[field] = marked[field]
    unmarked = _with_marked_message_count(reordered)
    assert "_row_layout" not in unmarked
    path = session_store / "rec-unmarked.json"
    path.write_text(json.dumps(unmarked, ensure_ascii=False, indent=2), encoding="utf-8")
    assert DB._read_metadata_head(path) is None
    source = db._read_metadata_row_source(path)
    full = db._read_path(path)
    assert isinstance(source, dict) and isinstance(full, dict)
    assert db._metadata_row("rec-unmarked", source) == db._metadata_row("rec-unmarked", full)
    assert db._metadata_row("rec-unmarked", source)["title"] == "Recovery Marked"


def test_unreadable_sidecar_falls_back_to_its_own_row(session_store, monkeypatch):
    """A sidecar that fails to open must skip its row, not raise out of the list.

    ``_read_metadata_head`` catches OSError; the budgeted-reader fallback opens
    the file again, so it needs the same boundary.
    """
    _save_modern_session(session_store, sid="healthy", messages=[{"role": "user", "content": "hi"}])
    doomed = session_store / "doomed.json"
    doomed.write_text("{}", encoding="utf-8")

    real_budgeted = models._read_metadata_json_prefix

    def _unreadable(path, *args, **kwargs):
        if Path(path).name == "doomed.json":
            raise FileNotFoundError(str(path))
        return real_budgeted(path, *args, **kwargs)

    monkeypatch.setattr(models, "_read_metadata_json_prefix", _unreadable)
    monkeypatch.setattr(DB, "_read_metadata_head", classmethod(lambda cls, p: None))

    assert DB._read_metadata_row_source(doomed) is None
    rows = DB(session_store).list_sessions()
    assert [row["session_id"] for row in rows] == ["healthy"]


def test_malformed_sidecar_is_skipped(session_store):
    (session_store / "broken.json").write_text("{not json", encoding="utf-8")
    _save_modern_session(session_store, sid="healthy", messages=[{"role": "user", "content": "hi"}])

    assert DB._read_metadata_row_source(session_store / "broken.json") is None
    rows = DB(session_store).list_sessions()
    assert [row["session_id"] for row in rows] == ["healthy"]


def test_head_derived_row_keeps_the_eager_checkpoint_fact(session_store):
    """A head-derived row must carry the field PR #8368 relies on.

    ``_pending_hidden_wakeup_at`` tells the sidebar that the pending hidden
    wakeup row was already checkpointed and therefore already excluded from the
    visible count. ``_metadata_row`` projects an explicit allowlist, so a key
    added to the payload without being added to that allowlist is silently
    dropped by every head/budgeted-prefix read - and the sidebar then discounts
    the hidden row a second time, which is exactly the undercount the field
    exists to prevent. This pins the two halves together so the allowlist
    cannot drift from the payload again.
    """
    db = DB(session_store)
    payload = {
        "session_id": "wakeup",
        "message_count": 2,
        "messages": [{"role": "user", "content": "one"}, {"role": "assistant", "content": "two"}],
        "title": "Eager Checkpoint",
        "pending_user_message": "Do the task.",
        "pending_started_at": 1234.5,
        "_pending_hidden_wakeup_at": 1234.5,
    }
    db.write_session(payload)
    path = session_store / "wakeup.json"

    full = db._read_path(path)
    assert isinstance(full, dict)
    full_row = db._metadata_row("wakeup", full)
    assert full_row.get("_pending_hidden_wakeup_at") == 1234.5, (
        "the full read must expose the fact; without it this test proves nothing"
    )

    source = db._read_metadata_row_source(path)
    assert isinstance(source, dict)
    head_row = db._metadata_row("wakeup", source)
    assert head_row is not None, "the payload must stay admissible through the head read"
    assert head_row.get("_pending_hidden_wakeup_at") == 1234.5, (
        "the head read dropped the eager-checkpoint fact, which reinstates the "
        "double discount in the sidebar"
    )
    assert head_row == full_row, "the head row must equal the full row"
