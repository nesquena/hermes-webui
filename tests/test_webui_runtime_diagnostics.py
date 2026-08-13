from api.config import STREAMS, STREAMS_LOCK, create_stream_channel
from api.routes import _stream_runtime_diagnostics


def test_stream_channel_exposes_buffer_and_subscriber_counts():
    channel = create_stream_channel()
    channel.put_nowait(("token", {"text": "offline"}))

    snapshot = channel.diagnostic_snapshot()
    # offline_buffered_bytes (#6351) is an ESTIMATE, so it is asserted as a
    # positive gauge rather than an exact value; the rest of the contract is
    # still exact.
    buffered_bytes = snapshot.pop("offline_buffered_bytes")
    assert buffered_bytes > 0
    assert snapshot == {
        "subscriber_count": 0,
        "offline_buffered_events": 1,
        "offline_dropped_events": 0,
        "subscriber_dropped_events": 0,
    }

    subscriber = channel.subscribe()
    try:
        snapshot = channel.diagnostic_snapshot()
        assert snapshot["subscriber_count"] == 1
        assert snapshot["offline_buffered_events"] == 1
        assert subscriber.get_nowait()[0] == "token"
    finally:
        channel.unsubscribe(subscriber)


def test_stream_runtime_diagnostics_summarizes_active_stream_channels():
    channel = create_stream_channel()
    channel.put_nowait(("token", {"text": "offline"}))
    subscriber = channel.subscribe()
    try:
        with STREAMS_LOCK:
            previous = dict(STREAMS)
            STREAMS.clear()
            STREAMS["stream-one"] = channel
        try:
            payload = _stream_runtime_diagnostics()
        finally:
            with STREAMS_LOCK:
                STREAMS.clear()
                STREAMS.update(previous)

        assert payload["active_streams"] == 1
        assert payload["total_subscribers"] == 1
        assert payload["total_offline_buffered_events"] == 1
        # Byte gauge (#6351) is an estimate: assert it is present, positive, and
        # consistent between the per-stream row and the total.
        assert payload["total_offline_buffered_bytes"] > 0
        assert len(payload["streams"]) == 1
        row = dict(payload["streams"][0])
        row_bytes = row.pop("offline_buffered_bytes")
        assert row_bytes == payload["total_offline_buffered_bytes"]
        assert row == {
            "stream_id": "stream-one",
            "subscriber_count": 1,
            "offline_buffered_events": 1,
        }
    finally:
        channel.unsubscribe(subscriber)
