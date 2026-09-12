"""Regression tests: GLM CoT-leak title shapes that escaped the old sanitizer.

Two live failures drove these:
- Session f28499888000: "The user's question is in English. I need a 3-8 word
  title matching the language" — possessive 'user's' defeated the old
  `^the user\s+` regex, and "I need a ..." (no 'to') defeated `I need to`.
- Session 5fba8d94e319 (Aug 26): 'Good title options: "A", "B", ...' —
  multi-option replies were never rejected.
"""

import unittest

from api.streaming import (
    _is_bad_new_title,
    _looks_invalid_generated_title,
    _sanitize_generated_title,
)


class TestPossessiveUserCoTLeak(unittest.TestCase):
    def test_the_users_question_leak_rejected(self):
        raw = "The user's question is in English. I need a 3-8 word title matching the language"
        self.assertEqual(_sanitize_generated_title(raw), "")

    def test_looks_invalid_catches_possessive_user(self):
        self.assertTrue(_looks_invalid_generated_title("The user's question is in English."))

    def test_looks_invalid_still_catches_plain_the_user(self):
        self.assertTrue(_looks_invalid_generated_title("The user is asking about widgets"))

    def test_looks_invalid_catches_i_need_without_to(self):
        self.assertTrue(_looks_invalid_generated_title("I need a short title for this"))

    def test_legitimate_title_not_rejected(self):
        self.assertEqual(
            _sanitize_generated_title("Respect Workspace Order in Chat Selector"),
            "Respect Workspace Order in Chat Selector",
        )

    def test_user_possessive_inside_topic_not_rejected(self):
        # 'user's' mid-title, not sentence-initial, should be fine.
        self.assertEqual(
            _sanitize_generated_title("Fixing the user's workspace selector"),
            "Fixing the user's workspace selector",
        )


class TestMultiOptionLeak(unittest.TestCase):
    def test_good_title_options_prefix_rejected(self):
        raw = 'Good title options: "GLM 5.3 Upgrade Vibes", "Testing GLM 5.3 Feel"'
        self.assertEqual(_sanitize_generated_title(raw), "")

    def test_two_quoted_alternatives_rejected(self):
        self.assertTrue(_is_bad_new_title('"Session Naming Bugs" or "GLM Title Leaks"'))

    def test_options_label_without_quotes_rejected(self):
        self.assertTrue(_is_bad_new_title("Title alternatives: Session Naming"))

    def test_single_quoted_phrase_still_allowed(self):
        self.assertEqual(
            _sanitize_generated_title('Understanding "quiet quitting" at work'),
            'Understanding "quiet quitting" at work',
        )


if __name__ == "__main__":
    unittest.main()
