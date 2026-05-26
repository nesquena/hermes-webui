from api.streaming import live_usage_prompt_estimate_after_tool_delta


def test_live_usage_estimate_caps_tool_delta_against_previous_prompt():
    usage = live_usage_prompt_estimate_after_tool_delta(
        base_prompt_tokens=86_723,
        exact_prompt_tokens=86_723,
        messages=[{"role": "tool", "content": "x" * 80_000}],
    )

    assert usage["last_prompt_tokens"] <= 86_723 + 12_000
    assert usage["last_prompt_tokens"] < 120_000


def test_live_usage_estimate_preserves_real_prompt_when_exact_prompt_advances():
    usage = live_usage_prompt_estimate_after_tool_delta(
        base_prompt_tokens=86_723,
        exact_prompt_tokens=136_000,
        messages=[{"role": "tool", "content": "x" * 80_000}],
    )

    assert usage["last_prompt_tokens"] == 136_000
    assert usage["turn_tool_prompt_tokens"] == 0


def test_live_usage_estimate_caps_cumulative_tool_delta_per_turn():
    from api import streaming

    base_prompt_tokens = 86_723
    turn_tool_prompt_tokens = 0
    usage = None
    original_delta = streaming._bounded_live_tool_prompt_delta
    streaming._bounded_live_tool_prompt_delta = lambda messages, cap=12_000: int(cap or 0)

    try:
        for _ in range(20):
            usage = live_usage_prompt_estimate_after_tool_delta(
                base_prompt_tokens=base_prompt_tokens,
                exact_prompt_tokens=base_prompt_tokens,
                messages=[{"role": "tool", "content": "x" * 80_000}],
                turn_tool_prompt_tokens=turn_tool_prompt_tokens,
            )
            turn_tool_prompt_tokens = usage["turn_tool_prompt_tokens"]
    finally:
        streaming._bounded_live_tool_prompt_delta = original_delta

    assert usage is not None
    assert usage["turn_tool_prompt_tokens"] == 24_000
    assert usage["last_prompt_tokens"] == base_prompt_tokens + 24_000
    assert usage["last_prompt_tokens"] < base_prompt_tokens + (20 * 12_000)
