from api.streaming import _restore_display_reasoning_metadata


def test_restore_display_reasoning_skips_empty_partial_activity_rows():
    previous = [
        {"role": "user", "content": "old turn", "timestamp": 1},
        {
            "role": "assistant",
            "content": "",
            "_partial": True,
            "timestamp": 2,
            "reasoning": "cancelled thinking",
            "_partial_tool_calls": [{"name": "terminal", "done": True}],
        },
        {"role": "user", "content": "new turn", "timestamp": 3},
        {"role": "assistant", "content": "new answer", "timestamp": 4},
    ]
    updated = [
        {"role": "user", "content": "old turn"},
        {"role": "user", "content": "new turn"},
        {"role": "assistant", "content": "new answer"},
    ]

    restored = _restore_display_reasoning_metadata(previous, updated)

    assert [m.get("content") for m in restored] == [
        "old turn",
        "new turn",
        "new answer",
    ]
    assert not any(m.get("_partial") for m in restored)


def test_restore_display_reasoning_keeps_non_partial_thinking_rows():
    previous = [
        {"role": "user", "content": "old turn", "timestamp": 1},
        {
            "role": "assistant",
            "content": "",
            "timestamp": 2,
            "reasoning": "visible thinking card",
        },
        {"role": "assistant", "content": "old answer", "timestamp": 3},
    ]
    updated = [
        {"role": "user", "content": "old turn"},
        {"role": "assistant", "content": "old answer"},
    ]

    restored = _restore_display_reasoning_metadata(previous, updated)

    assert restored[1]["reasoning"] == "visible thinking card"
    assert restored[2]["content"] == "old answer"
