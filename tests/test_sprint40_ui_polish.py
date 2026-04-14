"""
Sprint 40 UI Polish Tests: Telegram badge muted color and source tag formatting (issue #442).

Covers:
- style.css: Telegram badge border-left-color and ::after color use rgba(0, 136, 204, 0.55)
- style.css: Telegram badge rules no longer use fully-saturated #0088cc
- sessions.js: _formatSourceTag helper function is defined
- sessions.js: _formatSourceTag maps 'telegram' to 'via Telegram'
- sessions.js: metaBits push uses _formatSourceTag
"""
import pathlib
import re
import unittest

REPO_ROOT = pathlib.Path(__file__).parent.parent
STYLE_CSS = (REPO_ROOT / "static" / "style.css").read_text()
SESSIONS_JS = (REPO_ROOT / "static" / "sessions.js").read_text()


class TestTelegramBadgeMutedColor(unittest.TestCase):

    def test_telegram_badge_uses_muted_color(self):
        """Telegram badge rules must use rgba(0, 136, 204, 0.55) not #0088cc."""
        # Extract only the telegram-related CSS block
        telegram_lines = [
            line for line in STYLE_CSS.splitlines()
            if 'data-source="telegram"' in line or "data-source='telegram'" in line
        ]
        self.assertTrue(
            len(telegram_lines) >= 2,
            "Expected at least 2 telegram badge CSS rules"
        )
        muted_color = "rgba(0, 136, 204, 0.55)"
        for line in telegram_lines:
            self.assertIn(
                muted_color, line,
                f"Telegram CSS rule should use {muted_color!r}, got: {line!r}"
            )
            self.assertNotIn(
                "#0088cc", line,
                f"Telegram CSS rule must not use saturated #0088cc, got: {line!r}"
            )

    def test_telegram_border_left_color_muted(self):
        """The border-left-color rule for telegram uses rgba."""
        pattern = r'\.session-item\.cli-session\[data-source=["\']telegram["\']\]\s*\{[^}]*border-left-color:\s*rgba\(0,\s*136,\s*204,\s*0\.55\)'
        self.assertRegex(STYLE_CSS, pattern,
            "border-left-color for telegram should be rgba(0, 136, 204, 0.55)")

    def test_telegram_after_color_muted(self):
        """The ::after color rule for telegram uses rgba."""
        pattern = r'\.session-item\.cli-session\[data-source=["\']telegram["\']\]::after\s*\{[^}]*color:\s*rgba\(0,\s*136,\s*204,\s*0\.55\)'
        self.assertRegex(STYLE_CSS, pattern,
            "::after color for telegram should be rgba(0, 136, 204, 0.55)")


class TestFormatSourceTagHelper(unittest.TestCase):

    def test_format_source_tag_helper_exists(self):
        """_formatSourceTag function must be defined in sessions.js."""
        self.assertIn("function _formatSourceTag(", SESSIONS_JS,
            "_formatSourceTag helper function not found in sessions.js")

    def test_format_source_tag_maps_telegram(self):
        """_formatSourceTag maps 'telegram' to 'via Telegram'."""
        self.assertIn("telegram:'via Telegram'", SESSIONS_JS,
            "sessions.js should map telegram -> 'via Telegram'")

    def test_format_source_tag_maps_discord(self):
        """_formatSourceTag maps 'discord' to 'via Discord'."""
        self.assertIn("discord:'via Discord'", SESSIONS_JS,
            "sessions.js should map discord -> 'via Discord'")

    def test_format_source_tag_maps_slack(self):
        """_formatSourceTag maps 'slack' to 'via Slack'."""
        self.assertIn("slack:'via Slack'", SESSIONS_JS,
            "sessions.js should map slack -> 'via Slack'")

    def test_metabits_uses_format_helper(self):
        """The metaBits push for source_tag should use _formatSourceTag."""
        self.assertIn("metaBits.push(_formatSourceTag(s.source_tag))", SESSIONS_JS,
            "metaBits push should wrap source_tag with _formatSourceTag()")

    def test_raw_source_tag_not_pushed_directly(self):
        """The old raw metaBits.push(s.source_tag) should not exist."""
        self.assertNotIn("metaBits.push(s.source_tag)", SESSIONS_JS,
            "Raw s.source_tag should not be pushed directly to metaBits")


if __name__ == "__main__":
    unittest.main()
