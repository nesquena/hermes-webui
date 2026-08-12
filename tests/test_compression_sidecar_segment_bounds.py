"""Regression coverage for cumulative compression sidecar amplification."""

import json

from api import models, routes, streaming


def _msg(role, content, timestamp):
    return {"role": role, "content": content, "timestamp": timestamp}


def test_repeated_compression_persists_each_sidecar_as_one_segment(tmp_path, monkeypatch):
    monkeypatch.setattr(models, "SESSION_DIR", tmp_path)
    monkeypatch.setattr(models, "SESSION_INDEX_FILE", tmp_path / "_index.json")
    monkeypatch.setattr(streaming, "SESSION_DIR", tmp_path)
    models.SESSIONS.clear()

    root_messages = [_msg("user", "root prompt", 1), _msg("assistant", "root answer", 2)]
    middle_messages = [_msg("user", "middle prompt", 3), _msg("assistant", "middle answer", 4)]
    tip_messages = [_msg("user", "tip prompt", 5), _msg("assistant", "tip answer", 6)]

    root = models.Session(
        session_id="segment_root",
        messages=root_messages,
        pre_compression_snapshot=True,
    )
    root.save(touch_updated_at=False)

    # Incident shape: the live continuation already contains the stitched parent
    # transcript, rather than only its own canonical segment.
    middle = models.Session(
        session_id="segment_middle",
        parent_session_id=root.session_id,
        messages=root_messages + middle_messages,
    )
    middle.save(touch_updated_at=False)
    middle.messages = root_messages + middle_messages + tip_messages

    full_display = streaming._bound_compression_rotation_sidecars(
        middle,
        old_sid="segment_middle",
        previous_messages=root_messages + middle_messages,
    )
    middle.session_id = "segment_tip"
    middle.parent_session_id = "segment_middle"
    middle.pre_compression_snapshot = False
    streaming._apply_compression_live_segment(middle)
    middle.save(touch_updated_at=False)

    archived_middle = json.loads((tmp_path / "segment_middle.json").read_text(encoding="utf-8"))
    live_tip = json.loads((tmp_path / "segment_tip.json").read_text(encoding="utf-8"))

    assert [m["content"] for m in archived_middle["messages"]] == ["middle prompt", "middle answer"]
    assert [m["content"] for m in live_tip["messages"]] == ["tip prompt", "tip answer"]
    assert [m["content"] for m in full_display] == [
        "root prompt",
        "root answer",
        "middle prompt",
        "middle answer",
        "tip prompt",
        "tip answer",
    ]
    assert len(archived_middle["messages"]) + len(live_tip["messages"]) == 4
    stitched = routes._webui_sidecar_lineage_messages_for_display(
        models.Session.load("segment_tip")
    )
    assert [m["content"] for m in stitched] == [m["content"] for m in full_display]


def test_done_payload_can_project_full_display_without_reinflating_live_tip():
    session = models.Session(
        session_id="segment_payload",
        messages=[_msg("user", "tip only", 3)],
    )
    full_display = [
        _msg("user", "ancestor", 1),
        _msg("assistant", "ancestor answer", 2),
        *session.messages,
    ]

    payload = streaming._session_payload_with_full_messages(
        session,
        messages=full_display,
    )

    assert payload["messages"] == full_display
    assert payload["message_count"] == 3
    assert session.messages == [_msg("user", "tip only", 3)]


def test_bounding_fails_closed_when_previous_history_is_not_a_prefix(tmp_path, monkeypatch):
    monkeypatch.setattr(models, "SESSION_DIR", tmp_path)
    monkeypatch.setattr(models, "SESSION_INDEX_FILE", tmp_path / "_index.json")
    monkeypatch.setattr(streaming, "SESSION_DIR", tmp_path)
    models.SESSIONS.clear()

    previous = [_msg("user", "must not be lost", 1)]
    session = models.Session(session_id="segment_ambiguous", messages=previous + [_msg("assistant", "answer", 2)])
    session.save(touch_updated_at=False)

    full_display = streaming._bound_compression_rotation_sidecars(
        session,
        old_sid="segment_ambiguous",
        previous_messages=[_msg("user", "different history", 9)],
    )

    saved = json.loads((tmp_path / "segment_ambiguous.json").read_text(encoding="utf-8"))
    assert saved["messages"] == previous + [_msg("assistant", "answer", 2)]
    assert session.messages == previous + [_msg("assistant", "answer", 2)]
    assert full_display == session.messages
