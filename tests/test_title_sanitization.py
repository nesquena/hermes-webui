import unittest

from api.streaming import _sanitize_generated_title


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
