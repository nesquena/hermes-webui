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
            "all set",
            STREAMING_PY,
            "streaming.py should reject generic English completion phrases as session titles",
        )
        self.assertIn(
            "completed",
            STREAMING_PY,
            "streaming.py should reject completion-status titles as session titles",
        )
        self.assertNotIn(
            "测试完成",
            STREAMING_PY,
            "streaming.py title generation should stay English-only",
        )

    def test_streaming_uses_reasoning_split_for_minimax_titles(self):
        self.assertIn(
            "reasoning_split",
            STREAMING_PY,
            "streaming.py should request MiniMax title calls with reasoning_split so final text is separated from thinking",
        )

    def test_streaming_emits_title_sse_event(self):
        # After the stream_end fix, title uses original session_id param (not s.session_id
        # which can be rotated during context compression — see #652 fix)
        self.assertIn(
            "put_event('title', {'session_id': session_id, 'title': effective_title})",
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

    def test_title_snippet_uses_visible_assistant_reply_after_tools(self):
        """Tool-heavy opening turns should use the final visible assistant reply."""
        from api.streaming import _first_exchange_snippets

        user_msg = {
            "role": "user",
            "content": "Please look up the earlier context and then summarize it.",
        }
        preamble_asst = {
            "role": "assistant",
            "content": "Let me check my memory first.",
            "tool_calls": [
                {
                    "id": "call-1",
                    "function": {
                        "name": "memory",
                        "arguments": '{"action":"search"}',
                    },
                }
            ],
        }
        tool_result = {
            "role": "tool",
            "tool_call_id": "call-1",
            "content": '{"result":"background info"}',
        }
        final_asst = {
            "role": "assistant",
            "content": "Here is the substantive answer after the tool work.",
        }

        user_text, assistant_text = _first_exchange_snippets(
            [user_msg, preamble_asst, tool_result, final_asst]
        )

        self.assertEqual(user_text, user_msg["content"][:500])
        self.assertEqual(assistant_text, final_asst["content"][:500])

    def test_title_snippet_keeps_short_substantive_assistant_reply(self):
        """Short but real assistant answers should still be eligible for titles."""
        from api.streaming import _first_exchange_snippets

        messages = [
            {"role": "user", "content": "Can you help me rename this session?"},
            {"role": "assistant", "content": "Sure."},
        ]

        user_text, assistant_text = _first_exchange_snippets(messages)

        self.assertEqual(user_text, "Can you help me rename this session?")
        self.assertEqual(assistant_text, "Sure.")

    def test_provisional_title_detection_ignores_whitespace_noise(self):
        """Temporary first-message titles should still match with whitespace normalization."""
        from api.streaming import _is_provisional_title, title_from

        messages = [
            {
                "role": "user",
                "content": "过去两个礼拜发生了一些事情。最重要的一点就是我加入了一个 Hermes Web UI 的项目。\n\n因为我开始使用 Hermes 这个 agent 以后，就逐渐不再使用 OpenClaw了。",
            },
            {"role": "assistant", "content": "Sure, let me help."},
        ]

        derived = title_from(messages, "")
        current = derived[:63]  # Simulate the provisional title the UI writes immediately.

        self.assertNotEqual(current, derived[:64])
        self.assertTrue(
            _is_provisional_title(current, messages),
            "Whitespace-normalized provisional titles should still be recognized",
        )

    def test_title_snippet_keeps_tool_call_with_substantive_text(self):
        """An assistant row with tool_calls AND a substantive answer text
        must still be used as the first-exchange snippet — it's not a
        preamble, it's an agentic first-turn plan."""
        from api.streaming import _first_exchange_snippets

        user_msg = {
            "role": "user",
            "content": "Can you schedule a reminder for the Q3 kickoff meeting?",
        }
        # Assistant row with both a real answer AND a tool_call
        agentic_asst = {
            "role": "assistant",
            "content": "I'll schedule the Q3 kickoff reminder for next Monday at 9am.",
            "tool_calls": [
                {
                    "id": "call-1",
                    "function": {
                        "name": "cronjob",
                        "arguments": '{"action":"create","when":"mon 9am"}',
                    },
                }
            ],
        }

        user_text, assistant_text = _first_exchange_snippets([user_msg, agentic_asst])

        self.assertEqual(user_text, user_msg["content"][:500])
        self.assertEqual(
            assistant_text,
            agentic_asst["content"][:500],
            "Substantive answer text on a tool_call row must be preserved",
        )

    def test_fallback_title_preserves_unicode_letters(self):
        """Local fallback title generation must not strip German umlauts."""
        from api.streaming import _fallback_title_from_exchange

        title = _fallback_title_from_exchange(
            "Bitte führe ein Selbst-Audit durch. Wo ist überall noch Gemini-2.5-flash als Modell im Einsatz? Sei gründlich",
            "Ich prüfe live statt aus Bauchgefühl.",
        )

        self.assertIsNotNone(title)
        self.assertIn("führe", title)
        self.assertNotIn("hre", title.split())

    def test_title_snippet_skips_tool_call_preamble_only_rows(self):
        """Tool-call rows whose content is empty or meta-reasoning preamble
        ('Let me check my memory first.') must still be skipped — those are
        orchestration scaffolding, not title material."""
        from api.streaming import _first_exchange_snippets

        user_msg = {
            "role": "user",
            "content": "Summarize my notes from last week.",
        }
        empty_preamble = {
            "role": "assistant",
            "content": "",
            "tool_calls": [
                {
                    "id": "call-1",
                    "function": {
                        "name": "memory",
                        "arguments": '{"action":"search"}',
                    },
                }
            ],
        }
        meta_preamble = {
            "role": "assistant",
            "content": "Let me check my memory first.",
            "tool_calls": [
                {
                    "id": "call-2",
                    "function": {
                        "name": "memory",
                        "arguments": '{"action":"search","q":"last week"}',
                    },
                }
            ],
        }
        tool_result = {
            "role": "tool",
            "tool_call_id": "call-2",
            "content": '{"result":"background info"}',
        }
        final_asst = {
            "role": "assistant",
            "content": "Here's a summary of your notes from last week.",
        }

        _, assistant_text = _first_exchange_snippets(
            [user_msg, empty_preamble, meta_preamble, tool_result, final_asst]
        )

        self.assertEqual(
            assistant_text,
            final_asst["content"][:500],
            "Empty and meta-reasoning preamble rows must be skipped",
        )


if __name__ == "__main__":
    unittest.main()
