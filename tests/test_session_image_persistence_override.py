import base64
import json

import pytest

from api.models import Session
from api.session_image_persistence_override import (
    PERSISTENCE_OVERRIDE_VERSION,
    compact_persisted_image_parts,
)


def _data_url(raw: bytes, mime: str = "image/jpeg") -> str:
    return f"data:{mime};base64," + base64.b64encode(raw).decode("ascii")


def _openai_part(url: str, *, part_type: str = "image_url") -> dict:
    return {"type": part_type, "image_url": {"url": url}}


def _anthropic_part(raw: bytes, mime: str = "image/png") -> dict:
    return {
        "type": "image",
        "source": {
            "type": "base64",
            "media_type": mime,
            "data": base64.b64encode(raw).decode("ascii"),
        },
    }


@pytest.fixture
def session_dir(tmp_path, monkeypatch):
    import api.models as models
    from collections import OrderedDict

    directory = tmp_path / "sessions"
    directory.mkdir()
    monkeypatch.setattr(models, "SESSION_DIR", directory)
    monkeypatch.setattr(models, "SESSION_INDEX_FILE", directory / "_index.json")
    monkeypatch.setattr(models, "SESSIONS", OrderedDict())
    return directory


def test_override_compacts_user_and_tool_image_parts_for_persistence():
    huge = _data_url(b"A" * 1_000_000)
    messages = [
        {"role": "user", "content": [_openai_part(huge)]},
        {"role": "tool", "content": [_openai_part(huge)]},
    ]

    changed = compact_persisted_image_parts(messages)

    assert changed == 2
    assert "data:image" not in json.dumps(messages)
    assert all(m["content"] == [{"type": "text", "text": "[image attachment]"}] for m in messages)


def test_override_preserves_non_image_parts_and_is_idempotent():
    messages = [{"role": "user", "content": [
        {"type": "text", "text": "keep"},
        _openai_part(_data_url(b"AAAA", "image/png")),
        {"type": "document", "id": "doc-1"},
    ]}]

    assert compact_persisted_image_parts(messages) == 1
    assert messages[0]["content"] == [
        {"type": "text", "text": "keep"},
        {"type": "text", "text": "[image attachment]"},
        {"type": "document", "id": "doc-1"},
    ]
    assert compact_persisted_image_parts(messages) == 0


def test_responses_string_and_nested_urls_are_both_compacted():
    messages = [{"role": "user", "content": [
        {"type": "input_image", "image_url": _data_url(b"nested", "image/png")},
        _openai_part(_data_url(b"nested2", "image/png"), part_type="input_image"),
    ]}]

    assert compact_persisted_image_parts(messages) == 2
    assert "data:image" not in json.dumps(messages)


def test_anthropic_base64_source_images_are_compacted():
    messages = [{"role": "user", "content": [
        {"type": "text", "text": "what is this"},
        _anthropic_part(b"B" * 100_000),
    ]}]

    changed = compact_persisted_image_parts(messages)

    assert changed == 1
    assert "data:image" not in json.dumps(messages)
    assert messages[0]["content"][1] == {"type": "text", "text": "[image attachment]"}


def test_remote_references_and_malformed_parts_are_left_untouched():
    remote = _openai_part("https://example.test/cat.png")
    reference = _openai_part("/api/file/raw?session_id=s&path=cat.png")
    malformed_url = {"type": "image_url", "image_url": {"url": None}}
    malformed_source = {"type": "image", "source": {"type": "url", "url": "https://example.test/x.png"}}
    non_image_data = {"type": "text", "text": "data:image/png;base64,AAAA"}
    messages = [{"role": "user", "content": [remote, reference, malformed_url, malformed_source, non_image_data]}]

    assert compact_persisted_image_parts(messages) == 0
    assert messages[0]["content"] == [remote, reference, malformed_url, malformed_source, non_image_data]


def test_session_save_writes_compact_copy_without_mutating_model_payload(session_dir):
    huge = _data_url(b"A" * 1_000_000)
    source = [{"role": "user", "content": [_openai_part(huge)]}]
    session = Session(session_id="save-boundary", messages=source)

    session.save(touch_updated_at=False)

    persisted = json.loads((session_dir / "save-boundary.json").read_text())
    assert PERSISTENCE_OVERRIDE_VERSION == "1.1.0"
    assert "data:image" not in json.dumps(persisted["messages"])
    assert source[0]["content"][0]["image_url"]["url"] == huge
    assert session.messages == source


def test_compaction_runs_on_the_guarded_snapshot_not_the_live_transcript(session_dir, monkeypatch):
    """The replay guard owns the scan-time snapshot; compaction must not move it."""
    import api.models as models

    huge = _data_url(b"A" * 1_000_000)
    live_row = {"role": "user", "content": [_openai_part(huge)]}
    session = Session(session_id="snapshot-identity", messages=[live_row])
    original_messages = session.messages
    real_guard = models._deduplicate_exact_stable_messages
    scanned = []

    def recording_guard(messages):
        scanned.append(messages)
        return real_guard(messages)

    monkeypatch.setattr(models, "_deduplicate_exact_stable_messages", recording_guard)
    session.save(touch_updated_at=False)

    assert scanned == [original_messages]
    assert session.messages is original_messages
    persisted = json.loads((session_dir / "snapshot-identity.json").read_text())
    assert "data:image" not in json.dumps(persisted["messages"])


def test_save_keeps_rows_that_differ_only_by_image_bytes(session_dir):
    """Lossless dedup runs first: same identity + different bytes is two rows."""
    identity = {"role": "user", "id": "img-event-1", "timestamp": 10.5}
    first = {**identity, "content": [_openai_part(_data_url(b"first-image"))]}
    second = {**identity, "content": [_openai_part(_data_url(b"second-image"))]}
    session = Session(session_id="distinct-image-rows", messages=[first, second])

    session.save(touch_updated_at=False)

    persisted = json.loads((session_dir / "distinct-image-rows.json").read_text())
    assert len(persisted["messages"]) == 2
    assert persisted["message_count"] == 2
    assert "data:image" not in json.dumps(persisted)


def test_save_collapses_exact_duplicate_image_rows(session_dir):
    """Control for the previous test: byte-identical replays still collapse."""
    row = {"role": "user", "id": "img-event-2", "timestamp": 11.5,
           "content": [_openai_part(_data_url(b"same-image"))]}
    session = Session(session_id="duplicate-image-rows", messages=[row, json.loads(json.dumps(row))])

    session.save(touch_updated_at=False)

    persisted = json.loads((session_dir / "duplicate-image-rows.json").read_text())
    assert len(persisted["messages"]) == 1


def test_save_bounds_the_whole_json_with_a_populated_user_context(session_dir, tmp_path, monkeypatch):
    """Both histories must be bounded, and the live provider payload untouched."""
    import api.upload as upload

    attachments = tmp_path / "attachments"
    monkeypatch.setattr(upload, "STATE_DIR", tmp_path)
    monkeypatch.setenv("HERMES_WEBUI_ATTACHMENT_DIR", str(attachments))
    session_id = "context-bloat"
    inbox = attachments / session_id
    inbox.mkdir(parents=True)
    raw = b"\xff\xd8\xff\xe0" + b"J" * 750_000
    image_path = inbox / "photo.jpg"
    image_path.write_bytes(raw)

    from api.streaming import _build_native_multimodal_message

    provider_payload = _build_native_multimodal_message(
        "", "describe this", [{"path": str(image_path), "mime": "image/jpeg"}], str(tmp_path),
    )
    assert isinstance(provider_payload, list) and "data:image" in json.dumps(provider_payload)
    user_row = {"role": "user", "content": provider_payload, "attachments": ["photo.jpg"]}
    session = Session(session_id=session_id, messages=[user_row], context_messages=[json.loads(json.dumps(user_row))])

    session.save(touch_updated_at=False)

    text = (session_dir / f"{session_id}.json").read_text()
    assert "data:image" not in text
    assert len(text) < 200_000, f"persisted session JSON is {len(text)} bytes"
    persisted = json.loads(text)
    assert "data:image" not in json.dumps(persisted["context_messages"])
    assert "data:image" not in json.dumps(persisted["messages"])
    # The live provider payload still carries the bytes for the next model call.
    assert "data:image" in json.dumps(session.context_messages)
    assert "data:image" in json.dumps(session.messages)


def test_cold_reload_keeps_attachment_availability_and_bounded_bytes(session_dir, tmp_path, monkeypatch):
    import api.upload as upload

    attachments = tmp_path / "attachments"
    monkeypatch.setattr(upload, "STATE_DIR", tmp_path)
    monkeypatch.setenv("HERMES_WEBUI_ATTACHMENT_DIR", str(attachments))
    session_id = "reload-shape"
    inbox = attachments / session_id
    inbox.mkdir(parents=True)
    (inbox / "notes.jpg").write_bytes(b"K" * 400_000)

    huge = _data_url(b"K" * 400_000)
    row = {"role": "user", "content": [_openai_part(huge)], "attachments": ["notes.jpg"]}
    Session(session_id=session_id, messages=[row]).save(touch_updated_at=False)

    reloaded = Session.load(session_id)

    assert reloaded is not None
    assert reloaded.messages[0]["attachments"] == ["notes.jpg"]
    assert (inbox / "notes.jpg").is_file()
    assert "data:image" not in json.dumps(reloaded.messages)
    assert "data:image" not in json.dumps(reloaded.context_messages)


def test_distinct_image_rows_survive_repeated_cold_save_and_load(session_dir):
    """Projection must not erase the only content difference between two rows.

    The dedup guard fingerprints the whole row. If the projection collapses two
    rows with different image bytes to identical markers, the *next* save after
    a cold load reads them as exact replays and deletes one. A bounded digest of
    the dropped payload keeps them distinguishable across cycles.
    """
    identity = {"role": "user", "id": "cycle-event-1", "timestamp": 20.5}
    first = {**identity, "content": [_openai_part(_data_url(b"first-image"))]}
    second = {**identity, "content": [_openai_part(_data_url(b"second-image"))]}
    session = Session(session_id="cycle-distinct", messages=[first, second])

    for cycle in range(3):
        session.save(touch_updated_at=False)
        persisted = json.loads((session_dir / "cycle-distinct.json").read_text())
        assert len(persisted["messages"]) == 2, f"row lost on cycle {cycle}"
        assert persisted["message_count"] == 2
        assert "data:image" not in json.dumps(persisted)
        for row in persisted["messages"]:
            digest = row.get("_image_payload_digest")
            assert isinstance(digest, str) and 0 < len(digest) <= 40
        assert persisted["messages"][0]["_image_payload_digest"] != \
            persisted["messages"][1]["_image_payload_digest"]
        session = Session.load("cycle-distinct")
        assert len(session.messages) == 2


def test_exact_duplicate_image_rows_stay_collapsed_across_cycles(session_dir):
    row = {"role": "user", "id": "cycle-event-2", "timestamp": 21.5,
           "content": [_openai_part(_data_url(b"same-image"))]}
    session = Session(session_id="cycle-duplicate", messages=[row, json.loads(json.dumps(row))])

    for cycle in range(3):
        session.save(touch_updated_at=False)
        persisted = json.loads((session_dir / "cycle-duplicate.json").read_text())
        assert len(persisted["messages"]) == 1, f"duplicate survived on cycle {cycle}"
        session = Session.load("cycle-duplicate")


def test_provider_replay_rows_are_not_stamped_with_transcript_provenance(session_dir):
    """`_image_payload_digest` is a transcript-only key.

    Persisted `context_messages` are replayed to strict provider schemas; an
    unknown message key there could be rejected upstream. The discriminator is
    only needed where the lossless dedup guard runs (the display transcript).
    """
    huge = _data_url(b"A" * 500_000)
    row = {"role": "user", "id": "prov-1", "timestamp": 30.5, "content": [_openai_part(huge)]}
    session = Session(session_id="prov-shape", messages=[row],
                      context_messages=[json.loads(json.dumps(row))])

    session.save(touch_updated_at=False)

    persisted = json.loads((session_dir / "prov-shape.json").read_text())
    assert "_image_payload_digest" in persisted["messages"][0]
    assert all("_image_payload_digest" not in m for m in persisted["context_messages"])
    assert "data:image" not in json.dumps(persisted["context_messages"])
