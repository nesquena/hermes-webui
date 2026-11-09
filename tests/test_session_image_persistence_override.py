import base64
import json

import pytest

from api.models import Session
from api.session_image_persistence_override import (
    PERSISTENCE_OVERRIDE_VERSION,
    compact_persisted_image_parts,
)


def _b64_bytes(raw: bytes) -> str:
    return base64.b64encode(raw).decode("ascii")


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
    assert PERSISTENCE_OVERRIDE_VERSION == "1.2.0"
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


def test_save_bounds_the_display_transcript_and_keeps_replay_prefix_exact(session_dir, tmp_path, monkeypatch):
    """The display copy is bounded; the provider replay prefix stays byte-exact.

    `context_messages` is what the next model call replays, and an existing
    contract requires those rows to survive a save/load round trip unchanged
    (tests/test_cancel_restart_journal_recovery.py). Bounding it needs load-time
    rehydration, which this PR does not ship.
    """
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
    context_row = json.loads(json.dumps(user_row))
    session = Session(session_id=session_id, messages=[user_row], context_messages=[context_row])

    session.save(touch_updated_at=False)

    persisted = json.loads((session_dir / f"{session_id}.json").read_text())
    assert "data:image" not in json.dumps(persisted["messages"])
    assert persisted["context_messages"] == [context_row]
    # The live objects still carry the bytes for the next model call.
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
    # The replay prefix is persisted untouched: no marker, no provenance key.
    assert all("_image_payload_digest" not in m for m in persisted["context_messages"])
    assert "data:image" in json.dumps(persisted["context_messages"])


def test_api_content_sidecar_is_projected_too(session_dir):
    """The provider mirror duplicates the bytes; compaction must cover both."""
    import base64 as _b64

    raw = _b64.b64encode(b"\xff\xd8\xff\xe0" + b"X" * 200_000).decode()
    content = [
        {"type": "text", "text": "what is this"},
        {"type": "image_url", "image_url": {"url": "data:image/jpeg;base64," + raw}},
    ]
    api_content = json.dumps(content)
    row = {"role": "user", "content": content, "api_content": api_content}
    session = Session(session_id="apic", messages=[row],
                      context_messages=[json.loads(json.dumps(row))])

    session.save(touch_updated_at=False)

    text = (session_dir / "apic.json").read_text()
    assert "data:image" not in json.dumps(json.loads(text)["messages"]), "api_content mirror leaked inline bytes"
    persisted = json.loads(text)
    mirror = json.loads(persisted["messages"][0]["api_content"])
    assert mirror[0] == {"type": "text", "text": "what is this"}
    assert mirror[1] == {"type": "text", "text": "[image attachment]"}
    # The replay row keeps its provider-shaped mirror exactly as loaded.
    assert persisted["context_messages"][0]["api_content"] == api_content
    # The live row keeps its provider-shaped mirror for the next model call.
    assert json.loads(session.messages[0]["api_content"])[1]["image_url"]["url"].startswith("data:image/jpeg")


def test_non_image_api_content_is_left_byte_identical(session_dir):
    mirror = json.dumps([{"type": "text", "text": "plain"}, {"type": "text", "text": "tail"}])
    row = {"role": "user", "content": [{"type": "text", "text": "hi"}], "api_content": mirror}
    session = Session(session_id="apic-plain", messages=[row])

    session.save(touch_updated_at=False)

    persisted = json.loads((session_dir / "apic-plain.json").read_text())
    assert persisted["messages"][0]["api_content"] == mirror


def _cycle_session(session_dir, sid, rows, cycles=3):
    import api.models as models
    session = models.Session(session_id=sid, messages=rows)
    counts = []
    for _ in range(cycles):
        session.save(touch_updated_at=False)
        counts.append(json.loads((session_dir / f"{sid}.json").read_text())["message_count"])
        session = models.Session.load(sid)
    return counts


def test_rows_differing_only_by_image_detail_survive_cold_cycles(session_dir):
    """The marker erases the whole part, so the discriminator must cover structure.

    Two rows with identical bytes but a different accepted `image_url.detail` are
    distinct canonical rows; hashing only the payload string would make them equal
    after projection and the next save would delete one.
    """
    url = _data_url(b"identical-bytes")
    identity = {"role": "user", "id": "detail-event", "timestamp": 40.5}
    low = {**identity, "content": [{"type": "image_url", "image_url": {"url": url, "detail": "low"}}]}
    high = {**identity, "content": [{"type": "image_url", "image_url": {"url": url, "detail": "high"}}]}

    counts = _cycle_session(session_dir, "detail-cycle", [low, high])

    assert counts == [2, 2, 2], counts
    persisted = json.loads((session_dir / "detail-cycle.json").read_text())
    digests = {m["_image_payload_digest"] for m in persisted["messages"]}
    assert len(digests) == 2


@pytest.mark.parametrize("variant_a,variant_b", [
    ({"type": "image_url", "image_url": {"url": "SAME"}},
     {"type": "input_image", "image_url": {"url": "SAME"}}),
    ({"type": "image_url", "image_url": {"url": "SAME"}},
     {"type": "image_url", "image_url": {"url": "SAME"}, "cache_control": {"type": "ephemeral"}}),
])
def test_part_type_and_container_variants_survive_cold_cycles(session_dir, variant_a, variant_b):
    url = _data_url(b"same-payload-here")
    identity = {"role": "user", "id": "variant-event", "timestamp": 41.5}

    def materialize(variant):
        return {**identity, "content": [json.loads(json.dumps(variant).replace("SAME", url))]}

    counts = _cycle_session(session_dir, "variant-cycle", [materialize(variant_a), materialize(variant_b)])

    assert counts == [2, 2, 2], counts


def test_mirror_only_inline_image_is_bounded(session_dir):
    """Display content can be plain text while the mirror holds the only image."""
    raw = _b64_bytes(b"mirror-only")
    content = [{"type": "text", "text": "plain display"}]
    row = {"role": "user", "content": content,
           "api_content": json.dumps([{"type": "image_url", "image_url": {"url": "data:image/jpeg;base64," + raw}}])}
    session = Session(session_id="mirror-only", messages=[row])

    session.save(touch_updated_at=False)

    persisted = json.loads((session_dir / "mirror-only.json").read_text())
    assert "data:image" not in json.dumps(persisted["messages"])
    assert json.loads(persisted["messages"][0]["api_content"]) == [{"type": "text", "text": "[image attachment]"}]


def test_project_message_payloads_stamps_display_rows_by_default():
    """New-writer callers must inherit the display identity contract.

    Without the discriminator these writers turn legacy same-ID/timestamp rows
    with different bytes into equal marker rows: 2 immediately after the
    rewrite, 1 after the first cold save.
    """
    import base64 as _b64

    from api.session_image_persistence_override import project_message_payloads

    identity = {"role": "user", "id": "writer-event", "timestamp": 42.5}
    payload = {
        "session_id": "writer",
        "messages": [
            {**identity, "content": [_openai_part("data:image/jpeg;base64," + _b64.b64encode(b"first").decode())]},
            {**identity, "content": [_openai_part("data:image/jpeg;base64," + _b64.b64encode(b"second").decode())]},
        ],
    }

    projected = project_message_payloads(payload)

    assert projected is not payload
    digests = {m.get("_image_payload_digest") for m in projected["messages"]}
    assert None not in digests and len(digests) == 2
    # Provider-replay arrays still never receive the key.
    replay = {"context_messages": [dict(payload["messages"][0])]}
    assert all(
        "_image_payload_digest" not in m
        for m in project_message_payloads(replay, keys=("context_messages",))["context_messages"]
    )


def test_new_writer_rows_survive_a_later_cold_save(session_dir):
    """The writer stamps what it wrote, so the next save keeps both rows."""
    import api.models as models
    from api.session_image_persistence_override import project_message_payloads

    identity = {"role": "user", "id": "writer-cycle", "timestamp": 43.5}
    rows = [
        {**identity, "content": [_openai_part(_data_url(b"alpha"))]},
        {**identity, "content": [_openai_part(_data_url(b"beta"))]},
    ]
    payload = {"session_id": "writer-cycle", "message_count": 2, "messages": rows,
               "tool_calls": [], "anchor_activity_scenes": {}}
    projected = project_message_payloads(payload)
    (session_dir / "writer-cycle.json").write_text(json.dumps(projected))

    session = models.Session.load("writer-cycle")
    counts = []
    for _ in range(3):
        session.save(touch_updated_at=False)
        counts.append(json.loads((session_dir / "writer-cycle.json").read_text())["message_count"])
        session = models.Session.load("writer-cycle")

    assert counts == [2, 2, 2], counts


# --- Position and mirror-layout cold-cycle controls (maintainer supplement,
# 2026-11-03). Both lanes; real save -> cold load -> save. ---

_MARKER_TEXT = {"type": "text", "text": "[image attachment]"}


def _position_rows(identity, url, *, lane):
    """Two rows with identical bytes, differing only in WHERE the image sits."""
    if lane == "content":
        return [
            {**identity, "content": [_openai_part(url), dict(_MARKER_TEXT)]},
            {**identity, "content": [dict(_MARKER_TEXT), _openai_part(url)]},
        ]
    return [
        {**identity, "content": "plain", "api_content": json.dumps([_openai_part(url), dict(_MARKER_TEXT)])},
        {**identity, "content": "plain", "api_content": json.dumps([dict(_MARKER_TEXT), _openai_part(url)])},
    ]


@pytest.mark.parametrize("lane", ["content", "mirror"])
def test_image_position_is_preserved_across_cold_cycles(session_dir, lane):
    """The marker is position-independent, so the digest must carry the index.

    `[image, text]` and `[text, image]` are distinct rows; without the position in
    the digest they produce identical digests and collapse 2 -> 1 -> 1.
    """
    rows = _position_rows(
        {"role": "user", "id": f"pos-{lane}", "timestamp": 80.5},
        _data_url(b"identical-position-bytes"),
        lane=lane,
    )

    counts = _cycle_session(session_dir, f"pos-{lane}", rows)

    assert counts == [2, 2, 2], counts


def test_original_mirror_layout_is_preserved_across_cold_cycles(session_dir):
    """The mirror is reserialized canonically, so its ORIGINAL bytes must be hashed.

    Compact and indented JSON parse identically but are different persisted
    strings; the digest has to cover the original arrangement.
    """
    parts = [_openai_part(_data_url(b"identical-layout-bytes"))]
    compact = json.dumps(parts, separators=(",", ":"))
    indented = json.dumps(parts, indent=2)
    assert compact != indented and json.loads(compact) == json.loads(indented)

    identity = {"role": "user", "id": "layout", "timestamp": 81.5}
    rows = [
        {**identity, "content": "plain", "api_content": compact},
        {**identity, "content": "plain", "api_content": indented},
    ]

    counts = _cycle_session(session_dir, "layout", rows)

    assert counts == [2, 2, 2], counts


def test_preflight_recognises_every_shape_the_projector_accepts():
    """Early-out must not be narrower than the projector (mirror-only rows).

    The old literal `data:image/` scan missed Anthropic base64 mirrors, uppercase
    data URLs, and JSON-escaped slashes -- all of which the projector accepts.
    """
    from api.session_image_persistence_override import _has_inline_image

    raw = _b64_bytes(b"preflight-shape")
    escaped = ('[{"type":"image_url","image_url":{"url":"data:image\\/jpeg;base64,'
               + raw + '"}}]')
    assert "data:image/" not in escaped, "fixture must not contain the literal the old scan looked for"

    shapes = {
        "anthropic": json.dumps([_anthropic_part(b"preflight-shape")]),
        "uppercase": json.dumps([{"type": "image_url", "image_url": {"url": "DATA:IMAGE/JPEG;BASE64," + raw}}]),
        "json-escaped": escaped,
    }
    for label, mirror in shapes.items():
        assert _has_inline_image(mirror) is True, label

    # Clean mirrors must still early-out.
    assert _has_inline_image(json.dumps([{"type": "text", "text": "no image"}])) is False


# --- Proactive adversarial coverage: distinctions the projection erases must
# remain visible to the digest. Each pair shares id+timestamp and differs ONLY in
# the named property, so master keeps 2 -> 2 -> 2 and any hole collapses to
# 2 -> 1 -> 1. Verified to FAIL on b7ee91950 before the digest fixes. ---

def _adv_anth(raw, mime):
    return {"type": "image", "source": {"type": "base64", "media_type": mime,
                                        "data": base64.b64encode(raw).decode("ascii")}}


_ADVERSARIAL_PAIRS = {
    "anthropic_media_type_differs": lambda u: [
        _adv_anth(b"same-adv-bytes", "image/png"),
        _adv_anth(b"same-adv-bytes", "image/jpeg"),
    ],
    "anthropic_media_type_missing_vs_set": lambda u: [
        _adv_anth(b"same-adv-bytes", "image/png"),
        {"type": "image", "source": {"type": "base64", "data": base64.b64encode(b"same-adv-bytes").decode("ascii")}},
    ],
    "neighbouring_text_differs": lambda u: [
        [{"type": "text", "text": "AAA"}, _openai_part(u)],
        [{"type": "text", "text": "BBB"}, _openai_part(u)],
    ],
    "extra_part_key": lambda u: [
        _openai_part(u),
        {**_openai_part(u), "cache_control": {"type": "ephemeral"}},
    ],
    "subset_vs_superset_images": lambda u: [
        [_openai_part(u)],
        [_openai_part(u), _openai_part(_data_url(b"second-distinct-payload"))],
    ],
    "bare_string_url_vs_nested": lambda u: [
        [{"type": "image_url", "image_url": u}],
        [_openai_part(u)],
    ],
    "literal_marker_text_vs_real_image": lambda u: [
        [{"type": "text", "text": "[image attachment]"}],
        [_openai_part(u)],
    ],
}


@pytest.mark.parametrize("label", sorted(_ADVERSARIAL_PAIRS))
def test_erased_distinctions_stay_distinct_across_cold_cycles(session_dir, label):
    url = _data_url(b"shared-adversarial-payload")
    identity = {"role": "user", "id": f"adv-{label}", "timestamp": 90.5}
    first, second = _ADVERSARIAL_PAIRS[label](url)
    rows = [{**identity, "content": first}, {**identity, "content": second}]

    counts = _cycle_session(session_dir, f"adv-{label}", rows)

    assert counts == [2, 2, 2], f"{label} collapsed: {counts}"


def test_mirror_whitespace_difference_survives_cold_cycles(session_dir):
    """Mirrors differing only by surrounding whitespace are still distinct rows."""
    url = _data_url(b"shared-adversarial-payload")
    base = json.dumps([_openai_part(url)])
    identity = {"role": "user", "id": "adv-ws", "timestamp": 91.5}
    rows = [
        {**identity, "content": "plain", "api_content": base},
        {**identity, "content": "plain", "api_content": "  " + base + "  "},
    ]

    assert _cycle_session(session_dir, "adv-ws", rows) == [2, 2, 2]


def test_bracket_leading_non_json_mirror_is_left_untouched(session_dir):
    """Real agent rows carry bracket-leading PLAIN TEXT, not JSON.

    All 1061 api_content values sampled on this host are JSON arrays, but the
    detector must not mis-parse a value that merely starts with '['.
    """
    from api.session_image_persistence_override import _has_inline_image, _project_api_content
    import hashlib

    raw = "[Workspace::v1: /home/ubuntu/Hermes]\nplain text, not JSON"
    assert _has_inline_image(raw) is False
    assert _project_api_content(raw, hashlib.sha256()) is None


def test_partial_tool_call_image_bytes_are_bounded_across_cold_cycles(session_dir):
    """`_partial_tool_calls` is a persisted lane the content/mirror paths never visit.

    A captured screenshot-style tool call holds image parts inside `args`. Real
    session files on this host carry such bytes; unbounded they wrote 1,337,068
    bytes for five 200 KB images versus 3,147 for the same images in `content`.

    The projection must bound them while preserving every non-image field the
    interrupted-turn recovery and the UI read (name/done/duration/others).
    """
    big = _data_url(b"S" * 200_000)
    row = {
        "role": "assistant",
        "content": "partial text",
        "timestamp": 95.5,
        "_partial": True,
        "_partial_tool_calls": [
            {"name": "screenshot", "args": {"command": "capture", "parts": [
                {"type": "text", "text": "describe"},
                _openai_part(big),
            ]}, "done": True, "duration": 1.0},
            {"name": "read_file", "args": {"path": "/tmp/x.txt"}, "done": False},
        ],
    }
    session = Session(session_id="ptc", messages=[row])

    counts = []
    for _ in range(3):
        session.save(touch_updated_at=False)
        persisted = json.loads((session_dir / "ptc.json").read_text())
        counts.append(persisted["message_count"])
        session = Session.load("ptc")

    assert counts == [1, 1, 1]
    text = (session_dir / "ptc.json").read_text()
    assert "data:image" not in text, "partial tool-call image bytes reached disk"

    calls = json.loads(text)["messages"][0]["_partial_tool_calls"]
    assert calls[0]["args"]["parts"][1] == {"type": "text", "text": "[image attachment]"}
    # Every non-image field the recovery/UI path reads is preserved.
    assert calls[0]["name"] == "screenshot" and calls[0]["done"] is True
    assert calls[0]["args"]["command"] == "capture"
    assert calls[1] == {"name": "read_file", "args": {"path": "/tmp/x.txt"}, "done": False}


def test_partial_tool_call_nested_and_anthropic_images_are_bounded(session_dir):
    """Nesting depth and the Anthropic base64 shape must both be reached."""
    row = {
        "role": "assistant",
        "content": "partial",
        "timestamp": 96.5,
        "_partial_tool_calls": [{
            "name": "deep",
            "args": {"outer": {"inner": [
                _anthropic_part(b"A" * 50_000),
                {"type": "text", "text": "keep me"},
            ]}},
            "done": False,
        }],
    }
    session = Session(session_id="ptc-deep", messages=[row])

    session.save(touch_updated_at=False)

    text = (session_dir / "ptc-deep.json").read_text()
    assert "base64" not in text
    inner = json.loads(text)["messages"][0]["_partial_tool_calls"][0]["args"]["outer"]["inner"]
    assert inner[0] == {"type": "text", "text": "[image attachment]"}
    assert inner[1] == {"type": "text", "text": "keep me"}


def test_projector_leaves_clean_partial_tool_calls_untouched():
    """Copy-on-write: a partial tool-call list with no image bytes is not copied."""
    from api.session_image_persistence_override import project_message_payloads

    payload = {"messages": [{"role": "assistant", "content": "x",
                             "_partial_tool_calls": [{"name": "t", "args": {"a": 1}}]}]}
    assert project_message_payloads(payload) is payload


def test_distinct_partial_tool_call_images_do_not_collapse(session_dir):
    """Bounding the bytes must not make two distinct partial rows look identical.

    Projecting `args` replaces the image with a shared marker, so the tool
    signature becomes equal; only `_image_payload_digest` keeps them apart. Without
    it the collapse guard would delete one row on the next save.
    """
    identity = {"role": "assistant", "content": "partial", "timestamp": 97.5, "_partial": True}
    rows = []
    for payload in (b"first-partial-image", b"second-partial-image"):
        rows.append({**identity, "_partial_tool_calls": [{
            "name": "screenshot", "done": True,
            "args": {"parts": [_openai_part(_data_url(payload))]},
        }]})

    counts = _cycle_session(session_dir, "ptc-distinct", rows)

    assert counts == [2, 2, 2], counts
    persisted = json.loads((session_dir / "ptc-distinct.json").read_text())
    digests = {m.get("_image_payload_digest") for m in persisted["messages"]}
    assert None not in digests and len(digests) == 2


def _partial_row(args, when):
    return {
        "role": "assistant",
        "content": "partial",
        "timestamp": when,
        "_partial": True,
        "_partial_tool_calls": [{"name": "t", "args": args, "done": False}],
    }


def _cold_cycle_counts(session_dir, session_id, rows, cycles=3):
    session = Session(session_id=session_id, messages=rows)
    counts = []
    for _ in range(cycles):
        session.save(touch_updated_at=False)
        counts.append(json.loads((session_dir / f"{session_id}.json").read_text())["message_count"])
        session = Session.load(session_id)
    return counts


def test_dotted_key_does_not_alias_nested_traversal(session_dir):
    """A literal key `a.b` must not hash the same as the traversal a -> b.

    Path segments used to be joined with punctuation, so `{"a.b": image}` and
    `{"a": {"b": image}}` produced identical digest input. Two rows differing only
    in that placement then collapsed on a cold reload. Measured pre-fix:
    2 -> 1 -> 1.
    """
    image = _openai_part(_data_url(b"dotted"))
    marker = {"type": "text", "text": "[image attachment]"}
    rows = [
        _partial_row({"a.b": image, "a": {"b": marker}}, 11.0),
        _partial_row({"a.b": marker, "a": {"b": image}}, 11.0),
    ]

    counts = _cold_cycle_counts(session_dir, "ptc-dot", rows)

    assert counts == [2, 2, 2], counts


def test_bracket_key_does_not_alias_list_index(session_dir):
    """A literal key `a[0]` must not hash the same as list `a` at index 0."""
    image = _openai_part(_data_url(b"bracket"))
    marker = {"type": "text", "text": "[image attachment]"}
    rows = [
        _partial_row({"a[0]": image, "a": [marker]}, 12.0),
        _partial_row({"a[0]": marker, "a": [image]}, 12.0),
    ]

    counts = _cold_cycle_counts(session_dir, "ptc-brk", rows)

    assert counts == [2, 2, 2], counts


def test_ordinary_positional_swap_remains_distinct(session_dir):
    """Control: the plain position case the typed path must keep preserving."""
    image = _openai_part(_data_url(b"ordinary"))
    marker = {"type": "text", "text": "[image attachment]"}
    rows = [
        _partial_row({"p": [image, marker]}, 13.0),
        _partial_row({"p": [marker, image]}, 13.0),
    ]

    counts = _cold_cycle_counts(session_dir, "ptc-ord", rows)

    assert counts == [2, 2, 2], counts


def test_nested_list_swap_remains_distinct(session_dir):
    """Control: deeper nesting, position inside an inner list."""
    image = _openai_part(_data_url(b"nested-swap"))
    marker = {"type": "text", "text": "[image attachment]"}
    rows = [
        _partial_row({"a": {"b": [image, marker]}}, 14.0),
        _partial_row({"a": {"b": [marker, image]}}, 14.0),
    ]

    counts = _cold_cycle_counts(session_dir, "ptc-nst", rows)

    assert counts == [2, 2, 2], counts


def test_path_encoder_is_injective_on_adversarial_keys():
    """The encoder itself: distinct structured paths must never encode equal.

    Exercises the aliasing shapes directly, including keys crafted to mimic the
    separator/type-tag bytes, a length that could shift a boundary, and unicode.
    """
    from api.session_image_persistence_override import _encode_nested_path

    def enc(*segments):
        return _encode_nested_path(tuple(segments))

    pairs = [
        (enc(("k", "a.b")), enc(("k", "a"), ("k", "b"))),
        (enc(("k", "a[0]")), enc(("k", "a"), ("i", 0))),
        (enc(("k", "a"), ("i", 0)), enc(("k", "a"), ("k", "0"))),
        (enc(("k", "a\x00b")), enc(("k", "a"), ("k", "b"))),
        (enc(("k", "1")), enc(("i", 1))),
        (enc(("k", "ü")), enc(("k", "u"))),
        (enc(("k", "k"), ("k", "v")), enc(("k", "kv"))),
    ]
    for left, right in pairs:
        assert left != right, (left, right)

    # Identical paths must still encode identically.
    assert enc(("k", "a"), ("i", 1)) == enc(("k", "a"), ("i", 1))


def test_captured_tool_snippet_string_bytes_are_bounded(session_dir):
    """A tool-call `snippet` holds the data URL inside a plain STRING.

    Real session 24748c2f63ab.json carried 17 `vision_analyze` snippets, each a
    4,000-char Python ``repr`` of the provider payload holding a base64 data URL,
    while `args` pointed at a disk path. The dict and list branches of the
    nested projector never reach a bare string, so 57,303 base64 chars survived
    migration untouched.
    """
    url = _data_url(b"S" * 3_000)
    snippet = (
        "{'_multimodal': True, 'content': [{'type': 'text', 'text': 'look'}, "
        "{'type': 'image_url', 'image_url': {'url': '" + url + "'}}]}"
    )
    row = {
        "role": "assistant", "content": "p", "timestamp": 21.0, "_partial": True,
        "_partial_tool_calls": [
            {"name": "vision_analyze", "args": {"image_url": "/tmp/x.jpg"},
             "snippet": snippet, "done": False},
        ],
    }
    session = Session(session_id="snippet", messages=[row])
    session.save(touch_updated_at=False)

    text = (session_dir / "snippet.json").read_text()
    assert "data:image" not in text, "snippet base64 reached disk"

    kept = json.loads(text)["messages"][0]["_partial_tool_calls"][0]
    # Non-payload text is preserved verbatim: the repr's quoting and structure
    # must survive, because it is a captured record, not a JSON document.
    masked = kept["snippet"]
    assert masked.startswith("{'_multimodal': True, 'content': [{'type': 'text', 'text': 'look'},")
    assert masked.endswith("'image_url': {'url': '<inline-image>'}}]}")
    assert kept["name"] == "vision_analyze"
    assert kept["args"] == {"image_url": "/tmp/x.jpg"}


def test_short_data_url_mention_is_not_masked():
    """A string merely mentioning `data:image/` in prose must be left alone."""
    from api.session_image_persistence_override import _mask_inline_string

    prose = "The payload looked like data:image/png;base64,AAAA and that was it."
    assert _mask_inline_string(prose) is None
    # A JSON *part list* string is handled by the part-lane projector, not here.
    assert _mask_inline_string(json.dumps([_openai_part(_data_url(b"x" * 300))])) is not None


def _snippet_row(urls, *, stable_id, when=31.0):
    """A partial assistant row whose captured snippet holds several data URLs.

    The snippet is a Python ``repr`` preview, matching the production shape.
    """
    parts = ", ".join(
        "{'type': 'image_url', 'image_url': {'url': '%s'}}" % u for u in urls
    )
    snippet = (
        "{'_multimodal': True, 'content': [{'type': 'text', 'text': 'look'}, "
        + parts
        + "]}"
    )
    return {
        "role": "assistant",
        "id": stable_id,
        "timestamp": when,
        "content": "partial",
        "_partial_tool_calls": [{"name": "vision_analyze", "snippet": snippet, "done": False}],
    }


def test_multi_url_snippet_survives_cold_cycles(session_dir):
    """Several data URLs in ONE snippet must all be masked in a single save pass.

    `_mask_inline_string` used ``subn(..., count=1)``, so a save masked only the
    first qualifying URL. The second URL was masked on a LATER save, by which time
    the digest had already been stamped from the partially-projected string -- so
    distinctions erased by the first save were lost and a distinct row was deleted.
    Reviewer-measured pre-fix: 2 -> 2 -> 1 -> 1.
    """
    shared = _data_url(b"C" * 400)
    rows = [
        _snippet_row([_data_url(b"A" * 400), shared], stable_id="evt-shared"),
        _snippet_row([_data_url(b"B" * 400), shared], stable_id="evt-shared"),
    ]
    # Two rows differ only in their FIRST image; both share the later one.
    counts = _cold_cycle_counts(session_dir, "multi", rows, cycles=4)
    assert counts == [2, 2, 2, 2], f"a distinct row was deleted: {counts}"


def test_all_snippet_urls_removed_in_one_save(session_dir):
    """One save must leave NO qualified data URL behind in a snippet."""
    urls = [_data_url(b"D" * 400), _data_url(b"E" * 400), _data_url(b"F" * 400)]
    session = Session(session_id="one-pass", messages=[_snippet_row(urls, stable_id="s1")])
    session.save(touch_updated_at=False)

    text = (session_dir / "one-pass.json").read_text()
    assert "data:image" not in text, "a save left image bytes behind"
    # Surrounding capture text is still preserved verbatim.
    snippet = json.loads(text)["messages"][0]["_partial_tool_calls"][0]["snippet"]
    assert snippet.startswith("{'_multimodal': True, 'content': [{'type': 'text', 'text': 'look'},")


def test_three_url_snippet_cold_cycles(session_dir):
    """Three URLs, differing in the first, must also preserve both rows."""
    shared_a, shared_b = _data_url(b"G" * 400), _data_url(b"H" * 400)
    rows = [
        _snippet_row([_data_url(b"I" * 400), shared_a, shared_b], stable_id="evt3"),
        _snippet_row([_data_url(b"J" * 400), shared_a, shared_b], stable_id="evt3"),
    ]
    assert _cold_cycle_counts(session_dir, "three", rows, cycles=4) == [2, 2, 2, 2]


def test_single_url_snippet_still_preserves_rows(session_dir):
    """The already-working one-URL case must not regress."""
    rows = [
        _snippet_row([_data_url(b"K" * 400)], stable_id="evt1"),
        _snippet_row([_data_url(b"L" * 400)], stable_id="evt1"),
    ]
    assert _cold_cycle_counts(session_dir, "single", rows, cycles=4) == [2, 2, 2, 2]


def test_identical_snippet_rows_still_collapse(session_dir):
    """Exact duplicates must STILL collapse -- the guard's purpose is not defeated."""
    url = _data_url(b"M" * 400)
    rows = [
        _snippet_row([url], stable_id="dup"),
        _snippet_row([url], stable_id="dup"),
    ]
    counts = _cold_cycle_counts(session_dir, "dup", rows, cycles=3)
    # Measured on current head: the lossless replay guard fires on the FIRST
    # save, so exact duplicates are already collapsed at counts[0]. Asserting
    # counts[0] == 2 was my wrong expectation, not a bug.
    assert counts == [1, 1, 1], f"duplicate collapse broken: {counts}"


def test_clean_snippet_is_not_mutated(session_dir):
    """A snippet with no inline bytes is returned untouched (non-mutating control)."""
    from api.session_image_persistence_override import _mask_inline_string

    prose = "{'content': [{'type': 'text', 'text': 'no images here'}]}"
    assert _mask_inline_string(prose) is None


def test_state_db_prefixed_string_content_is_bounded(session_dir):
    """`content` stored in the Agent's sentinel form must still be compacted.

    Real `state.db` rows reach the recovery and discoverability writers with
    `content` as a ``\\x00json:``-prefixed JSON string. The projector only handled
    a list-valued `content`, so recovery persisted the full image bytes and the
    wiring this PR adds never bounded them.
    """
    import base64 as _b64

    payload = _b64.b64encode(b"Z" * 40_000).decode()
    parts = [
        {"type": "text", "text": "what is this"},
        {"type": "image_url", "image_url": {"url": "data:image/png;base64," + payload}},
    ]
    stored = "\x00json:" + json.dumps(parts, separators=(",", ":"))
    row = {"role": "user", "content": stored, "timestamp": 41.0, "id": "state-db-row"}

    session = Session(session_id="statedb", messages=[row])
    session.save(touch_updated_at=False)

    text = (session_dir / "statedb.json").read_text()
    assert payload not in text, "sentinelled content kept its inline image bytes"

    kept = json.loads(text)["messages"][0]["content"]
    assert isinstance(kept, str) and kept.startswith("\x00json:"), "storage form was not preserved"
    decoded = json.loads(kept[len("\x00json:"):])
    assert decoded[0]["text"] == "what is this", "the visible text was lost"


def test_sentinelled_content_without_images_is_untouched(session_dir):
    """A sentinelled list with no image parts must round-trip byte-identically."""
    stored = "\x00json:" + json.dumps([{"type": "text", "text": "just words"}])
    session = Session(session_id="clean-sentinel", messages=[
        {"role": "user", "content": stored, "timestamp": 42.0, "id": "r1"},
    ])
    session.save(touch_updated_at=False)
    kept = json.loads((session_dir / "clean-sentinel.json").read_text())["messages"][0]["content"]
    assert kept == stored


def test_sentinelled_anthropic_mirror_is_bounded_through_projection():
    """`project_message_payloads` must bound a sentinelled Anthropic base64 row.

    The preflight inspected the first character, which for a sentinelled value is
    the NUL of ``\\x00json:`` rather than ``[``. It therefore fell through to the
    substring scan and returned False for an Anthropic base64 mirror, which holds
    no ``data:image/`` text at all -- so the projector was never invoked and the
    recovery writers still wrote the payload.
    """
    import base64 as _b64

    from api.session_image_persistence_override import (
        _has_inline_image,
        project_message_payloads,
    )

    payload = _b64.b64encode(b"Q" * 20_000).decode()
    parts = [
        {"type": "text", "text": "describe"},
        {"type": "image", "source": {"type": "base64", "media_type": "image/png", "data": payload}},
    ]
    stored = "\x00json:" + json.dumps(parts, separators=(",", ":"))
    envelope = {"messages": [{"role": "user", "content": stored, "timestamp": 43.0, "id": "s1"}]}

    assert _has_inline_image(envelope["messages"]) is True
    projected = project_message_payloads(envelope)
    assert projected is not envelope
    assert payload not in json.dumps(projected)


def test_anchor_activity_scenes_snippets_are_bounded():
    """A saved scene snippet must not persist inline image bytes.

    `Session.save()` serializes `anchor_activity_scenes` verbatim, and a vision
    tool records its preview inside `scene.activity_rows[].tool.snippet` and
    `.payload.snippet`. Those lanes are NOT `messages`/`context_messages`, so
    `compact_persisted_image_parts` never visited them: the live store still held
    ~122 KB of >=256-char base64 in this one field across four sessions after
    migration, e.g. 24748c2f63ab.json.
    """
    from api.session_image_persistence_override import compact_persisted_anchor_scenes

    payload = _b64_bytes(b"Q" * 4000)
    snippet = f"{{'type': 'image_url', 'image_url': {{'url': 'data:image/jpeg;base64,{payload}'}}}}"
    scenes = {
        "a" * 64: {
            "scene": {"activity_rows": [{"tool": {"snippet": snippet}, "payload": {"snippet": snippet}}]},
            "message_ref": "b" * 64,
            "message_index": 7,
            "stream_id": "s-1",
            "updated_at": 1234.5,
            "version": 3,
        }
    }

    projected, changed = compact_persisted_anchor_scenes(scenes)

    assert changed is True, "scene snippet base64 was not bounded"
    assert payload not in json.dumps(projected)
    # Copy-on-write: the live object must be untouched so the render path and the
    # scene-key fingerprint keep reading the original bytes.
    assert payload in json.dumps(scenes), "live object was mutated"
    # Identity fields and the key map must survive verbatim, or the read path's
    # ref/index matching and _anchor_scene_index_from_records would drift.
    record = projected["a" * 64]
    assert sorted(projected.keys()) == sorted(scenes.keys())
    assert record["message_ref"] == "b" * 64
    assert record["message_index"] == 7
    assert record["stream_id"] == "s-1"
    assert record["updated_at"] == 1234.5
    assert record["version"] == 3


def test_clean_anchor_scenes_are_returned_unchanged():
    """No inline bytes -> the SAME object, so a clean save stays byte-identical."""
    from api.session_image_persistence_override import compact_persisted_anchor_scenes

    scenes = {"k" * 64: {"scene": {"activity_rows": [{"tool": {"snippet": "plain text"}}]}}}
    projected, changed = compact_persisted_anchor_scenes(scenes)
    assert changed is False
    assert projected is scenes


def test_anchor_scene_fingerprint_is_stable_across_projection():
    """The #5854 sidebar fingerprint must not move when snippets are bounded."""
    from api.models import _anchor_scene_index_from_records
    from api.session_image_persistence_override import compact_persisted_anchor_scenes

    payload = _b64_bytes(b"Q" * 4000)
    scenes = {
        "c" * 64: {
            "scene": {"activity_rows": [{"tool": {"snippet": f"data:image/png;base64,{payload}"}}]},
            "message_ref": "d" * 64,
            "updated_at": 99.0,
        }
    }
    before = _anchor_scene_index_from_records(scenes)
    projected, changed = compact_persisted_anchor_scenes(scenes)
    assert changed is True
    assert _anchor_scene_index_from_records(projected) == before


def test_plain_string_content_carrying_a_data_url_is_bounded():
    """A message whose `content` is a bare STRING holding a data URL.

    `_project_sentinelled_content` only handles the `\\x00json:` storage form, so a
    plain string fell through and was never compacted even though
    `_has_inline_image` reported it as carrying bytes. Not present in the live
    store today (verified: 0 occurrences across all 108 files), so this is a
    latent hole rather than an active leak -- closed here because it is the same
    unwired-lane class as the other four findings.
    """
    from api.session_image_persistence_override import compact_persisted_image_parts

    payload = _b64_bytes(b"V" * 4000)
    message = {"role": "user", "content": f"data:image/png;base64,{payload}", "id": "a", "timestamp": 1.0}

    changed = compact_persisted_image_parts([message])

    assert changed, "plain string data URL was not compacted"
    assert payload not in json.dumps(message)
    assert "<inline-image>" in message["content"]


def test_plain_string_without_images_is_untouched():
    """Ordinary prose must not be rewritten (no false positives)."""
    from api.session_image_persistence_override import compact_persisted_image_parts

    message = {"role": "user", "content": "just some ordinary text", "id": "b", "timestamp": 2.0}
    assert compact_persisted_image_parts([message]) == 0
    assert message["content"] == "just some ordinary text"


def test_text_part_data_url_is_bounded_in_the_same_pass():
    """A TEXT part holding a data URL must be masked on the FIRST save.

    Greptile P1 on a4...: `_project_parts` only masked image-typed parts, so a
    `{"type": "text", "text": "...data:image/...;base64,<long>..."}` part kept its
    URL after the image part was replaced by the marker. On the NEXT save
    `_project_sentinelled_content` returns None (nothing left to compact), so the
    plain-string fallback masked the leftover URL and re-stamped
    `_image_payload_digest` from a string that no longer distinguished two
    originally-different rows -- and the dedup guard then deleted one.
    """
    from api.session_image_persistence_override import compact_persisted_image_parts

    text_url = "data:image/png;base64," + _b64_bytes(b"T" * 2000)
    image = _b64_bytes(b"A" * 2000)
    parts = [
        {"type": "text", "text": "shared context " + text_url},
        {"type": "image_url", "image_url": {"url": "data:image/png;base64," + image}},
    ]
    message = {"role": "user", "content": "\x00json:" + json.dumps(parts), "id": "s", "timestamp": 1.0}

    compact_persisted_image_parts([message])

    assert text_url not in message["content"], "text-part data URL survived the first save"
    assert image not in message["content"]


def test_two_rows_differing_by_text_url_survive_repeated_saves():
    """End-to-end reproduction of the reported deletion.

    Two rows share `id`, `timestamp` and a text part, and differ only in their
    image payload. Row count must stay 2 across repeated cold saves; the pre-fix
    code produced 2 -> 2 -> 1 -> 1.
    """
    from api.models import _deduplicate_exact_stable_messages
    from api.session_image_persistence_override import compact_persisted_image_parts

    text_url = "data:image/png;base64," + _b64_bytes(b"T" * 2000)

    def row(image_bytes):
        parts = [
            {"type": "text", "text": "shared context " + text_url},
            {"type": "image_url", "image_url": {"url": "data:image/png;base64," + _b64_bytes(image_bytes)}},
        ]
        return {
            "role": "user",
            "content": "\x00json:" + json.dumps(parts),
            "id": "same-id",
            "timestamp": 5.0,
        }

    messages = [row(b"A" * 2000), row(b"B" * 2000)]
    counts = []
    for _ in range(4):
        guarded, _removed = _deduplicate_exact_stable_messages(messages)
        compact_persisted_image_parts(guarded)
        messages = guarded
        counts.append(len(messages))

    assert counts == [2, 2, 2, 2], f"distinct rows were collapsed: {counts}"
    digests = {m.get("_image_payload_digest") for m in messages}
    assert len(digests) == 2, "the two rows must keep distinct provenance"
