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
