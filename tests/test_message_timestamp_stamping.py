from api.streaming import _stamp_missing_message_timestamps


def test_stamp_missing_message_timestamps_uses_subsecond_sequence():
    messages = [
        {"role": "user", "content": "one"},
        {"role": "assistant", "content": "two"},
        {"role": "user", "content": "three"},
    ]

    stamped = _stamp_missing_message_timestamps(messages, now=1000.0)

    assert stamped == 3
    assert [m["timestamp"] for m in messages] == [1000.0, 1000.000001, 1000.000002]


def test_stamp_missing_message_timestamps_preserves_existing_timestamp_metadata():
    messages = [
        {"role": "user", "content": "old", "timestamp": 900.0},
        {"role": "assistant", "content": "synthetic", "_ts": 901.0},
        {"role": "user", "content": "new"},
    ]

    stamped = _stamp_missing_message_timestamps(messages, now=1000.0)

    assert stamped == 1
    assert messages[0]["timestamp"] == 900.0
    assert "timestamp" not in messages[1]
    assert messages[2]["timestamp"] == 1000.0
