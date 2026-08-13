"""
Regression tests for #6351 — StreamChannel's offline replay buffer is capped by
BYTES as well as by frame count.

``_OFFLINE_BUFFER_MAXLEN`` (#4633) bounds the number of buffered frames, which
bounds memory only if frames are small. Frame payloads are arbitrary objects —
tool results, base64 image data, large assistant messages — so a count-only cap
leaves the worst case unbounded: 8192 frames x an arbitrarily large payload.
That is the residual growth reported in #6351, where a backgrounded mobile/PWA
client leaves a long agentic turn running with ZERO subscribers and every frame
of that turn lands in the offline buffer.

``_OFFLINE_BUFFER_MAXBYTES`` now evicts oldest-first once the running estimate
exceeds the ceiling. Byte evictions count toward the SAME
``offline_dropped_events`` truncation signal as count evictions, so a
reconnecting tab still falls back to the run journal by ``last_event_id``.
"""
import logging

from api.config import (
    StreamChannel,
    _estimate_frame_bytes,
    create_stream_channel,
)


def _payload(nbytes: int) -> str:
    """A payload whose estimated size is at least ``nbytes``."""
    return "x" * nbytes


def test_offline_buffer_capped_by_bytes_well_under_frame_cap():
    """The byte ceiling holds even when the frame COUNT stays far below the
    count cap — the exact gap #6351 describes."""
    ch = create_stream_channel()
    chunk = 1024 * 1024  # 1 MiB per frame
    frames = (StreamChannel._OFFLINE_BUFFER_MAXBYTES // chunk) + 16

    for i in range(frames):
        ch.put_nowait(("token", _payload(chunk), f"id{i}"))

    snap = ch.diagnostic_snapshot()
    # Far below the frame-count cap, so the count cap alone would NOT have
    # evicted anything: this is purely the byte ceiling doing the work.
    assert len(ch._offline_buffer) < StreamChannel._OFFLINE_BUFFER_MAXLEN
    assert snap["offline_buffered_bytes"] <= StreamChannel._OFFLINE_BUFFER_MAXBYTES
    assert snap["offline_dropped_events"] > 0


def test_byte_eviction_drops_oldest_and_keeps_newest_tail():
    """Byte eviction is drop-oldest: the retained tail is the newest frames."""
    ch = create_stream_channel()
    chunk = 4 * 1024 * 1024  # 4 MiB
    frames = (StreamChannel._OFFLINE_BUFFER_MAXBYTES // chunk) + 5

    for i in range(frames):
        ch.put_nowait(("token", _payload(chunk), f"id{i}"))

    retained = list(ch._offline_buffer)
    assert retained, "buffer must never be fully drained by byte eviction"
    # Newest frame is always retained.
    assert retained[-1][2] == f"id{frames - 1}"
    # The head advanced past the evicted frames.
    assert retained[0][2] != "id0"


def test_running_byte_total_matches_retained_frames():
    """The running total stays exact across both eviction paths."""
    ch = create_stream_channel()
    chunk = 2 * 1024 * 1024
    frames = (StreamChannel._OFFLINE_BUFFER_MAXBYTES // chunk) + 8
    for i in range(frames):
        ch.put_nowait(("token", _payload(chunk), f"id{i}"))

    recomputed = sum(_estimate_frame_bytes(item) for item in ch._offline_buffer)
    assert ch._offline_bytes == recomputed
    assert len(ch._offline_frame_bytes) == len(ch._offline_buffer)


def test_single_oversized_frame_is_retained():
    """A lone frame larger than the whole ceiling must still be delivered —
    dropping the newest frame would break the retained-tail contract (and a
    terminal frame would strand the client)."""
    ch = create_stream_channel()
    huge = StreamChannel._OFFLINE_BUFFER_MAXBYTES * 2
    ch.put_nowait(("stream_end", _payload(huge), "id-final"))

    assert len(ch._offline_buffer) == 1
    assert ch._offline_buffer[0][2] == "id-final"
    # It is over the ceiling by construction; the invariant is retention, not
    # the ceiling, in this degenerate case.
    assert ch.diagnostic_snapshot()["offline_buffered_bytes"] > 0


def test_oversized_frame_evicts_prior_frames_but_survives():
    """One huge frame arriving after small ones evicts the small tail rather
    than being dropped itself."""
    ch = create_stream_channel()
    for i in range(50):
        ch.put_nowait(("token", _payload(1024), f"small{i}"))
    huge = StreamChannel._OFFLINE_BUFFER_MAXBYTES + 1024
    ch.put_nowait(("token", _payload(huge), "huge"))

    retained = list(ch._offline_buffer)
    assert retained[-1][2] == "huge"
    assert len(retained) == 1, "prior small frames should have been evicted"


def test_byte_eviction_sets_truncation_signal_for_reconnect():
    """A reconnecting subscriber must learn the tail was holed by the BYTE cap,
    not just the count cap, so it falls back to the run journal."""
    ch = create_stream_channel()
    chunk = 1024 * 1024
    frames = (StreamChannel._OFFLINE_BUFFER_MAXBYTES // chunk) + 10
    for i in range(frames):
        ch.put_nowait(("token", _payload(chunk), f"id{i}"))

    _q, snapshot = ch.subscribe_with_snapshot()
    assert snapshot["offline_dropped_events"] > 0
    # The replay window starts at the oldest RETAINED frame.
    assert snapshot["offline_first_event_id"] == ch._offline_buffer[0][2]
    assert snapshot["last_event_id"] == f"id{frames - 1}"


def test_live_broadcast_resets_byte_total():
    """The byte total resets with the buffer on the first live broadcast, so a
    later disconnect cycle starts clean."""
    ch = create_stream_channel()
    chunk = 1024 * 1024
    for i in range(8):
        ch.put_nowait(("token", _payload(chunk), f"id{i}"))
    assert ch._offline_bytes > 0

    q = ch.subscribe()
    ch.put_nowait(("token", "live", "id-live"))
    assert ch._offline_bytes == 0
    assert len(ch._offline_frame_bytes) == 0
    ch.unsubscribe(q)

    ch.put_nowait(("token", _payload(chunk), "id-after"))
    assert ch._offline_bytes == _estimate_frame_bytes(
        ("token", _payload(chunk), "id-after")
    )


def test_byte_eviction_logs_once_per_cycle(caplog):
    """The byte-ceiling eviction emits the one-shot per-cycle debug log."""
    ch = create_stream_channel()
    chunk = 4 * 1024 * 1024
    frames = (StreamChannel._OFFLINE_BUFFER_MAXBYTES // chunk) + 4
    with caplog.at_level(logging.DEBUG, logger="api.config"):
        for i in range(frames):
            ch.put_nowait(("token", _payload(chunk), f"id{i}"))
        assert caplog.text.count("over byte ceiling") == 1


def test_small_frames_unaffected_by_byte_cap():
    """Ordinary small-frame traffic must behave exactly as before: no byte
    eviction, no truncation signal."""
    ch = create_stream_channel()
    for i in range(500):
        ch.put_nowait(("token", f"delta-{i}", f"id{i}"))

    snap = ch.diagnostic_snapshot()
    assert snap["offline_buffered_events"] == 500
    assert snap["offline_dropped_events"] == 0
    assert snap["offline_buffered_bytes"] <= StreamChannel._OFFLINE_BUFFER_MAXBYTES


# ── _estimate_frame_bytes ────────────────────────────────────────────────────


def test_estimate_counts_string_payload():
    small = _estimate_frame_bytes(("token", "a", "id"))
    large = _estimate_frame_bytes(("token", "a" * 100_000, "id"))
    assert large - small >= 100_000 - 8


def test_estimate_counts_bytes_payload():
    assert _estimate_frame_bytes(b"a" * 50_000) >= 50_000


def test_estimate_counts_nested_dict_payload():
    """Realistic frames are dicts of strings — those must be accounted for, not
    charged a flat scalar floor."""
    payload = {"content": "z" * 200_000, "role": "assistant"}
    assert _estimate_frame_bytes(("message", payload, "id")) >= 200_000


def test_estimate_extrapolates_wide_containers():
    """A container wider than the sampling breadth is extrapolated, not
    under-reported (under-reporting is the unsafe direction)."""
    wide = ["y" * 1000 for _ in range(1000)]
    estimate = _estimate_frame_bytes(wide)
    # Sampling stops at _FRAME_SIZE_MAX_ITEMS but extrapolates to full length,
    # so the estimate must far exceed the sampled-only sum.
    assert estimate > 500_000


def test_estimate_is_bounded_for_deeply_nested_payloads():
    """Depth is capped so a pathological payload cannot make the estimator walk
    forever on the hot path."""
    deep = "leaf"
    for _ in range(200):
        deep = {"next": deep}
    # Must return promptly without recursion error.
    assert _estimate_frame_bytes(deep) > 0


def test_estimate_walk_is_node_bounded_on_hot_path():
    """Depth x breadth alone would allow ~16.7M visits per frame while holding
    the channel lock. A shared node budget must bound the WHOLE walk, so a wide
    nested payload stays fast."""
    import time

    # Nested wide containers: without a shared budget this explodes
    # combinatorially (64 * 64 * 64 ... per level).
    level3 = {f"k{i}": "v" * 64 for i in range(200)}
    level2 = {f"k{i}": dict(level3) for i in range(200)}
    level1 = {f"k{i}": dict(level2) for i in range(200)}

    start = time.perf_counter()
    estimate = _estimate_frame_bytes(("message", level1, "id"))
    elapsed = time.perf_counter() - start

    assert estimate > 0
    assert elapsed < 0.5, f"frame size estimate took {elapsed:.3f}s on the hot path"


def test_byte_total_exact_across_count_cap_eviction():
    """The running total must stay exact when the FRAME-COUNT cap is what
    evicts (deque(maxlen) drops the head silently, so its bytes are discounted
    ahead of the append). Regression guard for the two deques drifting."""
    ch = create_stream_channel()
    n = StreamChannel._OFFLINE_BUFFER_MAXLEN
    # Small frames so the byte ceiling never fires — isolate the count path.
    for i in range(n + 300):
        ch.put_nowait(("token", f"delta-{i}", f"id{i}"))

    assert len(ch._offline_buffer) == n
    assert len(ch._offline_frame_bytes) == n
    recomputed = sum(_estimate_frame_bytes(item) for item in ch._offline_buffer)
    assert ch._offline_bytes == recomputed
    assert ch._offline_bytes > 0


def test_estimate_survives_hostile_payload():
    """A payload whose iteration raises must not break event delivery."""

    class Hostile(dict):
        def items(self):
            raise RuntimeError("boom")

    assert _estimate_frame_bytes(Hostile()) > 0

    ch = create_stream_channel()
    ch.put_nowait(("token", Hostile(), "id-hostile"))
    assert len(ch._offline_buffer) == 1
