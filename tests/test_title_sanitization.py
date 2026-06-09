import unittest
from pathlib import Path

from api.streaming import (
    _fallback_title_from_exchange,
    _first_exchange_snippets,
    _sanitize_generated_title,
)


class TestGeneratedTitleSanitization(unittest.TestCase):
    def test_strips_session_title_markdown_prefix(self):
        self.assertEqual(
            _sanitize_generated_title("**Session Title:** Clarifying Topic for Discussion"),
            "Clarifying Topic for Discussion",
        )

    def test_strips_plain_title_prefix(self):
        self.assertEqual(
            _sanitize_generated_title("Title: Clarifying Topic for Discussion"),
            "Clarifying Topic for Discussion",
        )

    def test_strips_wrapping_markdown_emphasis(self):
        self.assertEqual(
            _sanitize_generated_title("**Clarifying Topic for Discussion**"),
            "Clarifying Topic for Discussion",
        )

    def test_first_exchange_skips_empty_assistant_tool_call_placeholder(self):
        messages = [
            {"role": "user", "content": "What time is it in San Francisco?"},
            {"role": "assistant", "content": "", "tool_calls": [{"id": "call_1"}]},
            {"role": "tool", "content": "tool output", "tool_call_id": "call_1"},
            {"role": "assistant", "content": "It is 6:16 PM in San Francisco."},
        ]
        self.assertEqual(
            _first_exchange_snippets(messages),
            ("What time is it in San Francisco?", "It is 6:16 PM in San Francisco."),
        )

    def test_fallback_title_uses_english_discussion_suffix(self):
        self.assertEqual(
            _fallback_title_from_exchange('Please review "random cancel"', ""),
            "random cancel discussion",
        )

    def test_fallback_title_summary_label_is_english(self):
        self.assertEqual(
            _fallback_title_from_exchange("Generate a short title summary test", ""),
            "Session title auto-summary test",
        )

    def test_fallback_title_non_latin_input_uses_english_placeholder(self):
        self.assertEqual(
            _fallback_title_from_exchange("讨论一下这个问题", ""),
            "Conversation topic",
        )

    def test_fallback_title_non_latin_quoted_topic_uses_english_placeholder(self):
        self.assertEqual(
            _fallback_title_from_exchange('Please review "讨论主题"', ""),
            "Conversation topic",
        )

    def test_title_generation_source_has_no_cjk_literals(self):
        src = Path("api/streaming.py").read_text(encoding="utf-8")
        self.assertNotRegex(src, r"[\u4e00-\u9fff]", "title generation code should stay English-only")
