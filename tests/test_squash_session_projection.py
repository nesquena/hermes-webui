from types import SimpleNamespace


def _message(role, content, timestamp, message_id=None, **extra):
    message = {
        "role": role,
        "content": content,
        "timestamp": timestamp,
    }
    if message_id is not None:
        message["id"] = message_id
    message.update(extra)
    return message


def test_squash_summary_watermark_blocks_older_state_db_replay():
    from api.routes import _merged_session_messages_for_display

    summary = _message(
        "assistant",
        "# Session compactée\n\nRésumé opérationnel vérifié.",
        200.0,
        message_id="squash-200",
        _squash_summary=True,
    )
    session = SimpleNamespace(
        session_id="fd05-copy",
        messages=[summary],
        session_source="webui",
        parent_session_id=None,
        truncation_watermark=200.0,
        truncation_boundary=200.0,
        compression_anchor_mode="manual",
    )
    state_db_messages = [
        _message("user", "ancien prompt", 100.0, message_id=1),
        _message("assistant", "ancienne réponse", 110.0, message_id=2),
    ]

    merged = _merged_session_messages_for_display(session, state_db_messages)

    assert merged == [summary]


def test_non_squash_short_sidecar_keeps_existing_merge_behavior():
    from api.routes import _merged_session_messages_for_display

    sidecar = _message("assistant", "nouvelle réponse", 200.0, message_id=3)
    session = SimpleNamespace(
        session_id="ordinary-session",
        messages=[sidecar],
        session_source="webui",
        parent_session_id=None,
        truncation_watermark=None,
        truncation_boundary=None,
        compression_anchor_mode=None,
    )
    state_db_messages = [
        _message("user", "ancien prompt", 100.0, message_id=1),
        _message("assistant", "ancienne réponse", 110.0, message_id=2),
    ]

    merged = _merged_session_messages_for_display(session, state_db_messages)

    assert [message["id"] for message in merged] == [1, 2, 3]
