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


def test_cold_load_flag_expands_window_to_fill_renderable_rows():
    """With expand_renderable=True, a tail with <limit renderables expands back.

    Tail window has 1 renderable (a9) + 4 tool rows. The existing blank-window
    fallback does NOT fire (there IS a renderable), so this exercises the NEW
    expansion path: it walks back to include 4 more renderable rows.
    """
    messages = [
        ({"role": "user", "content": f"u{i}"} if i % 2 == 0 else {"role": "assistant", "content": f"a{i}"})
        for i in range(10)
    ] + [
        {"role": "tool", "content": f"tool {idx}"}
        for idx in range(10, 14)
    ]

    window, offset = _message_window_for_display(messages, msg_limit=5, expand_renderable=True)

    # Expanded back to index 5 so the window holds 5 renderable rows (a5..a9).
    assert offset == 5
    assert [m["content"] for m in window if m["role"] != "tool"] == ["a5", "u6", "a7", "u8", "a9"]


def test_cumulative_load_earlier_counts_visible_rows_without_expand_flag():
    """The 'Load earlier' path counts user/assistant rows, not raw tool rows."""
    messages = [
        ({"role": "user", "content": f"u{i}"} if i % 2 == 0 else {"role": "assistant", "content": f"a{i}"})
        for i in range(10)
    ] + [
        {"role": "tool", "content": f"tool {idx}"}
        for idx in range(10, 14)
    ]

    # Same input as the cold-load test, but no expand flag (cumulative path).
    window, offset = _message_window_for_display(messages, msg_limit=5, expand_renderable=False)

    assert offset == 5
    assert [m["content"] for m in window] == ["a5", "u6", "a7", "u8", "a9"]


def test_cold_load_expands_but_caps_at_total_renderable():
    """Cold-load expansion stops at the session's total renderable count.

    When the whole session has fewer renderable rows than msg_limit, the
    backward walk must terminate at index 0 (not loop forever) and return the
    full source.
    """
    messages = [
        {"role": "user", "content": "only-user"},
    ] + [
        {"role": "tool", "content": f"tool {idx}"}
        for idx in range(8)
    ]

    window, offset = _message_window_for_display(messages, msg_limit=5, expand_renderable=True)

    # Only 1 renderable row in the whole session → expand back to index 0.
    assert offset == 0
    assert window[0]["content"] == "only-user"


def test_initial_msg_limit_keeps_matching_trailing_tool_result_row():
    """A role:tool result row whose tool_call_id matches the newest assistant
    tool-call must be retained in the paginated window — the renderer rebuilds
    tool cards from these rows (CLI-origin / empty S.toolCalls path), so dropping
    it leaves the card without its result snippet. (#4070 ship-review)
    """
    messages = [
        {"role": "user", "content": "do a thing"},
        {
            "role": "assistant",
            "content": "",
            "tool_calls": [
                {"id": "call_1", "function": {"name": "terminal", "arguments": "{}"}}
            ],
        },
        {"role": "tool", "tool_call_id": "call_1", "content": "the result output"},
    ]

    window, offset = _message_window_for_display(messages, msg_limit=30)

    assert [m["role"] for m in window] == ["user", "assistant", "tool"]
    assert any(
        m["role"] == "tool" and m.get("tool_call_id") == "call_1" for m in window
    )
    assert offset == 0


def test_initial_msg_limit_skips_orphan_trailing_tool_rows_without_match():
    """Trailing tool rows with NO matching tool-call in the window are still
    skipped (they don't consume the visible-row budget). Guards against the
    matching-tool fix over-extending the window. (#4070 ship-review)
    """
    messages = [
        {"role": "user", "content": "question"},
        {"role": "assistant", "content": "answer"},
    ] + [
        {"role": "tool", "tool_call_id": f"orphan_{idx}", "content": f"r{idx}"}
        for idx in range(40)
    ]

    window, offset = _message_window_for_display(messages, msg_limit=5)

    assert [m["role"] for m in window] == ["user", "assistant"]
    assert offset == 0
