from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def test_session_compact_exposes_prompt_cache_counters():
    from api.models import Session

    session = Session(
        session_id="issue2419_cache_usage",
        workspace="/tmp",
        input_tokens=120_000,
        output_tokens=5_000,
        estimated_cost=0.44,
        cache_read_tokens=100_000,
        cache_write_tokens=20_000,
    )

    compact = session.compact()

    assert compact["cache_read_tokens"] == 100_000
    assert compact["cache_write_tokens"] == 20_000


def test_streaming_usage_payload_includes_prompt_cache_counters():
    src = (ROOT / "api" / "streaming.py").read_text()

    assert "session_cache_read_tokens" in src
    assert "session_cache_write_tokens" in src
    assert "'cache_read_tokens': cache_read_tokens" in src
    assert "'cache_write_tokens': cache_write_tokens" in src


def test_context_indicator_surfaces_cache_hit_rate():
    src = (ROOT / "static" / "ui.js").read_text()

    assert "cacheReadTok=usage.cache_read_tokens||0" in src
    assert "cacheWriteTok=usage.cache_write_tokens||0" in src
    assert "cache: ${cacheHitPct}% hit" in src
    assert "Estimated cost: $${cost<0.01?cost.toFixed(4):cost.toFixed(2)}" in src
    assert "cache ${Math.round((cacheRead/cacheTotal)*100)}% hit" in src


def test_done_handler_preserves_per_turn_cache_deltas():
    src = (ROOT / "static" / "messages.js").read_text()

    assert "_prevCacheRead=(S.session&&S.session.cache_read_tokens)||0" in src
    assert "curCacheRead=d.usage.cache_read_tokens||0" in src
    assert "cache_read_tokens:Math.max(0,curCacheRead-_prevCacheRead)" in src
    assert "cache_write_tokens:Math.max(0,curCacheWrite-_prevCacheWrite)" in src
