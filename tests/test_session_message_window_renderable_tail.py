from api.routes import _message_window_for_display


def test_initial_msg_limit_skips_trailing_tool_only_rows():
    messages = [
        {"role": "user", "content": "question"},
        {"role": "assistant", "content": "answer"},
    ] + [
        {"role": "tool", "content": f"tool result {idx}"}
        for idx in range(40)
    ]

    window, offset = _message_window_for_display(messages, msg_limit=5)

    assert [m["role"] for m in window] == ["user", "assistant"]
    assert offset == 0


def test_initial_msg_limit_skips_trailing_empty_partial_activity_rows():
    messages = [
        {"role": "user", "content": "today question", "timestamp": 200},
        {"role": "assistant", "content": "today answer", "timestamp": 201},
    ] + [
        {
            "role": "assistant",
            "content": "",
            "_partial": True,
            "timestamp": 100,
            "reasoning": f"old cancelled thinking {idx}",
            "_partial_tool_calls": [{"name": "terminal", "done": True}],
        }
        for idx in range(40)
    ]

    window, offset = _message_window_for_display(messages, msg_limit=5)

    assert [m["content"] for m in window] == ["today question", "today answer"]
    assert offset == 0


def test_msg_limit_keeps_raw_tail_when_it_has_renderable_rows():
    messages = [
        {"role": "user", "content": f"u{idx}"} if idx % 2 == 0 else {"role": "assistant", "content": f"a{idx}"}
        for idx in range(10)
    ]

    window, offset = _message_window_for_display(messages, msg_limit=4)

    assert [m["content"] for m in window] == ["u6", "a7", "u8", "a9"]
    assert offset == 6


def test_msg_before_anchors_page_before_trailing_tool_rows():
    messages = [
        {"role": "user", "content": "older"},
        {"role": "assistant", "content": "visible before tools"},
    ] + [
        {"role": "tool", "content": f"hidden {idx}"}
        for idx in range(12)
    ] + [
        {"role": "assistant", "content": "newer visible"},
    ]

    window, offset = _message_window_for_display(messages, msg_limit=3, msg_before=14)

    assert [m["role"] for m in window] == ["user", "assistant"]
    assert [m["content"] for m in window] == ["older", "visible before tools"]
    assert offset == 0


def test_all_tool_session_keeps_tail_fallback():
    messages = [
        {"role": "tool", "content": f"tool {idx}"}
        for idx in range(6)
    ]

    window, offset = _message_window_for_display(messages, msg_limit=3)

    assert [m["content"] for m in window] == ["tool 3", "tool 4", "tool 5"]
    assert offset == 3
