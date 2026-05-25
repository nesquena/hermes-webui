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
