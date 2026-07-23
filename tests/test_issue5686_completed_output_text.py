from api import streaming


PRIOR_DISPLAY = [
    {"role": "user", "content": "Earlier request"},
    {"role": "assistant", "content": "Earlier reply"},
]


class TestIssue5686CompletedOutputText:
    def test_assistant_reply_added_after_current_turn_counts_visible_output_text_after_tool_metadata(self):
        previous_context = list(PRIOR_DISPLAY) + [
            {"role": "user", "content": "What is the weather?"},
        ]
        result_messages = list(previous_context) + [
            {
                "role": "assistant",
                "content": [
                    {"type": "tool_use", "name": "weather.lookup", "input": {"city": "Leeds"}},
                    {"type": "output_text", "output_text": "It is 18C and sunny."},
                ],
                "tool_calls": [{"id": "call_1", "type": "function", "function": {"name": "weather.lookup", "arguments": "{}"}}],
            }
        ]
        assert streaming._assistant_reply_added_after_current_turn(
            result_messages,
            previous_context,
            "What is the weather?",
        ) is True

    def test_session_lacks_final_answer_false_when_visible_output_text_follows_tool_use(self):
        messages = list(PRIOR_DISPLAY) + [
            {"role": "user", "content": "What is the weather?"},
            {
                "role": "assistant",
                "content": [
                    {"type": "tool_use", "name": "weather.lookup", "input": {"city": "Leeds"}},
                    {"type": "output_text", "output_text": "It is 18C and sunny."},
                ],
                "tool_calls": [{"id": "call_1", "type": "function", "function": {"name": "weather.lookup", "arguments": "{}"}}],
            },
        ]
        assert streaming._session_lacks_final_assistant_answer(messages) is False

    def test_session_lacks_final_answer_true_for_tool_only_assistant_content(self):
        messages = list(PRIOR_DISPLAY) + [
            {"role": "user", "content": "What is the weather?"},
            {
                "role": "assistant",
                "content": [
                    {"type": "tool_use", "name": "weather.lookup", "input": {"city": "Leeds"}},
                ],
                "tool_calls": [{"id": "call_1", "type": "function", "function": {"name": "weather.lookup", "arguments": "{}"}}],
            },
        ]
        assert streaming._session_lacks_final_assistant_answer(messages) is True

    def test_session_lacks_final_answer_false_for_untyped_visible_text_with_call_metadata(self):
        messages = list(PRIOR_DISPLAY) + [
            {"role": "user", "content": "What is the weather?"},
            {
                "role": "assistant",
                "content": [
                    {"call_id": "call_1", "name": "weather.lookup", "text": "It is 18C and sunny."},
                ],
            },
        ]
        assert streaming._session_lacks_final_assistant_answer(messages) is False

    def test_session_lacks_final_answer_true_for_untyped_tool_part_with_empty_args(self):
        messages = list(PRIOR_DISPLAY) + [
            {"role": "user", "content": "What is the weather?"},
            {
                "role": "assistant",
                "content": [
                    {"call_id": "call_1", "args": {}},
                ],
            },
        ]
        assert streaming._session_lacks_final_assistant_answer(messages) is True

    def test_session_lacks_final_answer_true_for_top_level_tool_calls_without_in_content_boundary(self):
        messages = list(PRIOR_DISPLAY) + [
            {"role": "user", "content": "What is the weather?"},
            {
                "role": "assistant",
                "content": [
                    {"type": "text", "text": "Checking the tool result"},
                ],
                "tool_calls": [{"id": "call_1", "type": "function", "function": {"name": "weather.lookup", "arguments": "{}"}}],
            },
        ]
        assert streaming._session_lacks_final_assistant_answer(messages) is True

    def test_session_lacks_final_answer_false_for_plain_output_text_parts(self):
        messages = list(PRIOR_DISPLAY) + [
            {"role": "user", "content": "Summarize"},
            {
                "role": "assistant",
                "content": [
                    {"type": "output_text", "output_text": "Summary complete."},
                ],
            },
        ]
        assert streaming._session_lacks_final_assistant_answer(messages) is False
