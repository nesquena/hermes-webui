"""
Sprint 40 UI Polish Tests: Active session title uses CSS theme variable (issue #440).

Covers:
- .session-item.active .session-title uses var(--gold) instead of hardcoded #e8a030
- The hardcoded amber color #e8a030 is NOT present in the active session title rule
"""
import pathlib
import re
import unittest

REPO_ROOT = pathlib.Path(__file__).parent.parent
STYLE_CSS = (REPO_ROOT / "static" / "style.css").read_text()


class TestActiveSessionTitleThemeColor(unittest.TestCase):

    def test_active_session_title_uses_theme_variable(self):
        """
        .session-item.active .session-title must use var(--gold) not a hardcoded hex.
        The light-theme override line (data-theme="light") is allowed to keep its own
        hardcoded color; we only check the base/dark rule.
        """
        # Find all lines that match the active session title selector
        lines = STYLE_CSS.splitlines()
        base_rule_lines = [
            line for line in lines
            if ".session-item.active .session-title" in line
            and 'data-theme="light"' not in line
        ]

        self.assertTrue(
            len(base_rule_lines) >= 1,
            "Could not find .session-item.active .session-title base rule in style.css"
        )

        for line in base_rule_lines:
            self.assertIn(
                "var(--gold)",
                line,
                f"Expected var(--gold) in active session title rule, got: {line.strip()}"
            )
            self.assertNotIn(
                "#e8a030",
                line,
                f"Hardcoded #e8a030 must be removed from active session title rule: {line.strip()}"
            )


if __name__ == "__main__":
    unittest.main()
