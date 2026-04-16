"""
Sprint 41 Tests: Title auto-generation fix + mobile close button CSS (PR #333).

Covers:
- streaming.py: sessions titled 'New Chat' trigger auto-title generation
- streaming.py: sessions with empty/falsy title trigger auto-title generation
- streaming.py: sessions titled 'Untitled' (original guard) still trigger
- streaming.py: sessions with a user-set title do NOT trigger auto-title
- style.css: .mobile-close-btn is hidden by default (desktop rule present)
- style.css: .mobile-close-btn shown in <=900px media query
- style.css: #btnCollapseWorkspacePanel hidden in <=900px media query
- index.html: both .mobile-close-btn and #btnCollapseWorkspacePanel buttons exist
"""
import pathlib
import re
import unittest

REPO_ROOT = pathlib.Path(__file__).parent.parent
CSS = (REPO_ROOT / "static" / "style.css").read_text()
HTML = (REPO_ROOT / "static" / "index.html").read_text()
MESSAGES_JS = (REPO_ROOT / "static" / "messages.js").read_text()
STREAMING_PY = (REPO_ROOT / "api" / "streaming.py").read_text()


# ── streaming.py: title auto-generation condition ─────────────────────────

class TestTitleAutoGenerationCondition(unittest.TestCase):
    """Verify the guarded condition in streaming.py covers all default title cases."""

    def _titles_that_trigger(self):
        """Extract the condition from the source so tests stay in sync with code."""
        # Find the if-condition that calls title_from
        m = re.search(
            r'if\s+(s\.title\s*==.*?):\s*\n\s*s\.title\s*=\s*title_from',
            STREAMING_PY,
            re.DOTALL,
        )
        self.assertIsNotNone(m, "Could not find title auto-generation condition in streaming.py")
        return m.group(1)

    def test_untitled_in_condition(self):
        cond = self._titles_that_trigger()
        self.assertIn("'Untitled'", cond, "Original 'Untitled' guard must be present")

    def test_new_chat_in_condition(self):
        cond = self._titles_that_trigger()
        self.assertIn("'New Chat'", cond, "'New Chat' guard must be present (PR #333)")

    def test_empty_title_guard_in_condition(self):
        cond = self._titles_that_trigger()
        self.assertIn("not s.title", cond, "Empty/falsy title guard must be present (PR #333)")

    def test_condition_logic_covers_all_defaults(self):
        """The condition uses OR so any one default title triggers generation."""
        cond = self._titles_that_trigger()
        # All three guards must be joined by 'or'
        parts = re.split(r'\bor\b', cond)
        self.assertGreaterEqual(len(parts), 3,
            "Expected at least 3 OR-joined sub-conditions (Untitled, New Chat, not s.title)")


# ── style.css: mobile close button visibility ─────────────────────────────

class TestMobileCloseButtonCSS(unittest.TestCase):
    """Verify CSS rules that control the duplicate close button on mobile."""

    def test_mobile_close_btn_hidden_by_default(self):
        """Desktop default: .mobile-close-btn must be display:none outside any media query."""
        # Find the rule before the first @media block that contains mobile-close-btn
        # We look for the pattern in the desktop (non-media-query) section
        self.assertIn(
            ".mobile-close-btn{display:none;}",
            CSS.replace(" ", ""),
            ".mobile-close-btn should be hidden by default (desktop) — rule missing or wrong"
        )

    def test_mobile_close_btn_shown_in_900px_query(self):
        """Inside max-width:900px media query, .mobile-close-btn must be display:flex."""
        # Extract the 900px media block
        m = re.search(r'@media\s*\(max-width\s*:\s*900px\)\s*\{([^{}]*(?:\{[^{}]*\}[^{}]*)*)\}',
                      CSS)
        self.assertIsNotNone(m, "@media(max-width:900px) block not found in style.css")
        block = m.group(1).replace(" ", "")
        self.assertIn(".mobile-close-btn{display:flex;}",
                      block,
                      ".mobile-close-btn must be display:flex inside the 900px media query")

    def test_desktop_collapse_btn_hidden_in_900px_query(self):
        """Inside max-width:900px media query, #btnCollapseWorkspacePanel must be display:none."""
        m = re.search(r'@media\s*\(max-width\s*:\s*900px\)\s*\{([^{}]*(?:\{[^{}]*\}[^{}]*)*)\}',
                      CSS)
        self.assertIsNotNone(m, "@media(max-width:900px) block not found in style.css")
        block = m.group(1).replace(" ", "")
        self.assertIn("#btnCollapseWorkspacePanel{display:none;}",
                      block,
                      "#btnCollapseWorkspacePanel must be display:none in 900px media query")

    def test_900px_query_retains_existing_rules(self):
        """Ensure the PR didn't accidentally drop existing rules from the 900px block."""
        m = re.search(r'@media\s*\(max-width\s*:\s*900px\)\s*\{([^{}]*(?:\{[^{}]*\}[^{}]*)*)\}',
                      CSS)
        self.assertIsNotNone(m)
        block = m.group(1)
        self.assertIn("rightpanel", block, ".rightpanel rule missing from 900px block")
        self.assertIn("mobile-files-btn", block, ".mobile-files-btn rule missing from 900px block")


# ── index.html: button presence ───────────────────────────────────────────

class TestWorkspacePanelButtons(unittest.TestCase):
    """Verify both panel buttons are present in the HTML so CSS rules have targets."""

    def test_desktop_collapse_button_exists(self):
        self.assertIn("btnCollapseWorkspacePanel", HTML,
                      "#btnCollapseWorkspacePanel button must exist in index.html")

    def test_mobile_close_button_exists(self):
        self.assertIn("mobile-close-btn", HTML,
                      ".mobile-close-btn button must exist in index.html")

    def test_mobile_close_button_has_aria_label(self):
        """Accessibility: mobile close button must have an aria-label."""
        m = re.search(r'class="[^"]*mobile-close-btn[^"]*"[^>]*>', HTML)
        self.assertIsNotNone(m, "Could not find mobile-close-btn element")
        self.assertIn("aria-label", m.group(0),
                      "mobile-close-btn must have aria-label for accessibility")


class TestIssue495TitleStreaming(unittest.TestCase):
    """Regression checks for issue #495 title SSE behavior."""

    def test_streaming_has_llm_title_helper(self):
        self.assertIn(
            "def _generate_llm_session_title_for_agent(",
            STREAMING_PY,
            "streaming.py should define an agent-backed LLM title helper for session titles",
        )

    def test_streaming_rejects_generic_completion_titles(self):
        self.assertIn(
            "测试完成",
            STREAMING_PY,
            "streaming.py should reject generic completion phrases as session titles",
        )
        self.assertIn(
            "all set",
            STREAMING_PY,
            "streaming.py should reject generic English completion phrases as session titles",
        )

    def test_streaming_uses_reasoning_split_for_minimax_titles(self):
        self.assertIn(
            "reasoning_split",
            STREAMING_PY,
            "streaming.py should request MiniMax title calls with reasoning_split so final text is separated from thinking",
        )

    def test_streaming_emits_title_sse_event(self):
        self.assertIn(
            "put_event('title', {'session_id': s.session_id, 'title': s.title})",
            STREAMING_PY,
            "streaming.py should emit a title SSE event when title is updated",
        )

    def test_streaming_emits_title_status_sse_event(self):
        self.assertIn(
            "put_event('title_status', payload)",
            STREAMING_PY,
            "streaming.py should emit a title_status SSE event for title generation diagnostics",
        )

    def test_streaming_emits_stream_end_event(self):
        self.assertIn(
            "put_event('stream_end', {'session_id': session_id})",
            STREAMING_PY,
            "background title path should end the SSE stream with stream_end",
        )

    def test_frontend_listens_for_title_event(self):
        self.assertIn(
            "addEventListener('title'",
            MESSAGES_JS,
            "messages.js should listen for title SSE events",
        )

    def test_frontend_listens_for_title_status_event(self):
        self.assertIn(
            "addEventListener('title_status'",
            MESSAGES_JS,
            "messages.js should listen for title_status SSE events",
        )
        self.assertIn(
            "console.info('[title]'",
            MESSAGES_JS,
            "messages.js should log title generation diagnostics to the browser console",
        )

    def test_frontend_refreshes_title_ui_after_title_event(self):
        self.assertIn(
            "syncTopbar()",
            MESSAGES_JS,
            "messages.js title listener should sync top bar title",
        )
        self.assertTrue(
            ("renderSessionListFromCache()" in MESSAGES_JS) or ("renderSessionList()" in MESSAGES_JS),
            "messages.js title listener should refresh session list UI",
        )

    def test_frontend_waits_for_stream_end_before_closing(self):
        self.assertIn(
            "addEventListener('stream_end'",
            MESSAGES_JS,
            "messages.js should close SSE connection on stream_end (not immediately on done)",
        )


if __name__ == "__main__":
    unittest.main()
