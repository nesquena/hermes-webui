from api.models import Session


def _messages(count: int) -> list[dict]:
    return [
        {"role": "user" if i % 2 == 0 else "assistant", "content": f"message {i}", "timestamp": float(i + 1)}
        for i in range(count)
    ]


def test_compact_uses_actual_message_count_for_full_session_when_metadata_count_is_stale():
    session = Session(
        session_id="stale_count",
        title="Stale Count",
        messages=_messages(5),
        message_count=3,
    )

    assert session._metadata_message_count == 3
    assert not getattr(session, "_loaded_metadata_only", False)
    assert session.compact()["message_count"] == 5


def test_compact_preserves_metadata_count_for_metadata_only_stub():
    session = Session(
        session_id="metadata_only",
        title="Metadata Only",
        messages=[],
        message_count=7,
    )
    setattr(session, "_loaded_metadata_only", True)

    assert session.compact()["message_count"] == 7
