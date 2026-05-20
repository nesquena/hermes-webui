from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def test_webui_backend_prompt_cache_hit_percent_uses_prompt_total_denominator():
    from api.usage import prompt_cache_hit_percent

    assert prompt_cache_hit_percent(100_000, 125_000) == 80
    assert prompt_cache_hit_percent(0, 125_000) is None
    assert prompt_cache_hit_percent(100, 0) is None
    assert prompt_cache_hit_percent(None, None) is None
    assert prompt_cache_hit_percent(200, 100) == 100


def test_session_compact_exposes_prompt_cache_counters():
    from api.models import Session

    session = Session(
        session_id="issue2419_cache_usage",
        workspace="/tmp",
        input_tokens=125_000,
        output_tokens=5_000,
        estimated_cost=0.44,
        cache_read_tokens=100_000,
        cache_write_tokens=5_000,
    )

    compact = session.compact()

    assert compact["cache_read_tokens"] == 100_000
    assert compact["cache_write_tokens"] == 5_000
    assert compact["cache_hit_percent"] == 80


def test_streaming_usage_payload_includes_prompt_cache_counters():
    src = (ROOT / "api" / "streaming.py").read_text()

    assert "session_cache_read_tokens" in src
    assert "session_cache_write_tokens" in src
    assert "prompt_cache_hit_percent(" in src
    assert "'cache_hit_percent':" in src
    assert "'turn_cache_hit_percent':" in src


def test_context_indicator_surfaces_cache_hit_rate():
    src = (ROOT / "static" / "ui.js").read_text()

    assert "cacheReadTok=usage.cache_read_tokens||0" in src
    assert "cacheWriteTok=usage.cache_write_tokens||0" in src
    assert "cacheHitPct=usage.cache_hit_percent" in src
    assert "t('usage_cache_hit_detail',cacheHitPct" in src
    assert "Estimated cost: $${cost<0.01?cost.toFixed(4):cost.toFixed(2)}" in src
    assert "cacheHitPct=msg._turnUsage.cache_hit_percent" in src
    assert "t('usage_cached_percent',cacheHitPct)" in src
    assert "cacheHitPct!=null" in src
    assert "cacheReadTok/cacheTotalTok" not in src
    assert "cacheRead/cacheTotal" not in src
    assert "cacheReadTok/promptTok" not in src
    assert "cacheRead/cacheDenom" not in src


def test_cache_usage_labels_are_localized():
    src = (ROOT / "static" / "i18n.js").read_text()

    assert src.count("usage_cache_hit_detail:") == 11
    assert src.count("usage_cached_percent:") == 11
    assert "usage_cache_hit_detail: 'Cache: {0}% hit ({1} read / {2} write)'" in src
    assert "usage_cached_percent: '{0}% cached'" in src


def test_done_handler_preserves_per_turn_cache_deltas():
    src = (ROOT / "static" / "messages.js").read_text()

    assert "_prevCacheRead=(S.session&&S.session.cache_read_tokens)||0" in src
    assert "curCacheRead=d.usage.cache_read_tokens||0" in src
    assert "cache_read_tokens:Math.max(0,curCacheRead-_prevCacheRead)" in src
    assert "cache_write_tokens:Math.max(0,curCacheWrite-_prevCacheWrite)" in src
    assert "cache_hit_percent:d.usage.turn_cache_hit_percent" in src
