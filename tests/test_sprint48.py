"""Tests for sprint 48 UX bug fixes — v0.50.92.

Covers:
  - #702: XML tool-call syntax (<function_calls>) stripped from assistant
          message content before rendering (server-side + client-side).
  - #703: Workspace file panel shows an empty-state message when no workspace
          is configured or the directory is empty.
  - #704: Notification settings description uses "app" instead of "tab".
"""

import pathlib
import re

REPO = pathlib.Path(__file__).parent.parent


def read(rel):
    return (REPO / rel).read_text()


# ── Bug #702 — XML tool-call leak on DeepSeek ────────────────────────────────


class TestXmlToolCallStrip:
    """_strip_xml_tool_calls() is defined in api/streaming.py and must remove
    <function_calls>...</function_calls> blocks from assistant content."""

    def _load_fn(self):
        """Import the helper from streaming.py without triggering full server
        initialisation (which would fail in unit-test contexts)."""
        import sys
        import types

        # Stub heavy transitive imports so we can import the module cleanly.
        for mod in ("api.config", "api.helpers", "api.models", "api.workspace"):
            if mod not in sys.modules:
                sys.modules[mod] = types.ModuleType(mod)

        # Provide minimal symbols that streaming.py needs at import time.
        cfg = sys.modules.setdefault("api.config", types.ModuleType("api.config"))
        for attr in (
            "STREAMS",
            "STREAMS_LOCK",
            "CANCEL_FLAGS",
            "AGENT_INSTANCES",
            "LOCK",
            "SESSIONS",
            "SESSION_DIR",
            "_get_session_agent_lock",
            "_set_thread_env",
            "_clear_thread_env",
            "resolve_model_provider",
        ):
            if not hasattr(cfg, attr):
                setattr(cfg, attr, None)

        # Fall back to reading the source and exec-ing just the function.
        src = read("api/streaming.py")
        ns: dict = {}
        # Extract the function definition with regex so we don't need to import
        # the whole module (avoids all the heavy deps).
        match = re.search(
            r"(def _strip_xml_tool_calls\(.*?)\n(?=\ndef |\nclass )", src, re.DOTALL
        )
        assert match, "_strip_xml_tool_calls not found in api/streaming.py"
        exec(compile("import re\n" + match.group(1), "<streaming_extract>", "exec"), ns)
        return ns["_strip_xml_tool_calls"]

    def test_complete_block_removed(self):
        fn = self._load_fn()
        text = "Hello <function_calls><invoke>foo</invoke></function_calls> world"
        result = fn(text)
        assert "<function_calls>" not in result
        assert "Hello" in result
        assert "world" in result

    def test_orphaned_opening_tag_removed(self):
        fn = self._load_fn()
        text = "Some answer text\n<function_calls>\n<invoke>tool</invoke>"
        result = fn(text)
        assert "<function_calls>" not in result
        assert "Some answer text" in result

    def test_no_tag_unchanged(self):
        fn = self._load_fn()
        text = "This is a normal response with no tool calls."
        assert fn(text) == text

    def test_multiple_blocks_removed(self):
        fn = self._load_fn()
        text = (
            "Part one <function_calls><invoke>a</invoke></function_calls> "
            "middle <function_calls><invoke>b</invoke></function_calls> end"
        )
        result = fn(text)
        assert "<function_calls>" not in result
        assert "Part one" in result
        assert "middle" in result
        assert "end" in result

    def test_dsml_prefixed_truncated_opening_tag_removed(self):
        fn = self._load_fn()
        text = "Answer before tool tag <｜DSML｜function_calls"
        result = fn(text)
        assert "function_calls" not in result.lower()
        assert "Answer before tool tag" in result

    def test_malformed_dsml_fragment_removed(self):
        fn = self._load_fn()
        text = "Answer <｜DSML | still streaming"
        result = fn(text)
        assert "<｜DSML |" not in result
        assert "Answer" in result
        assert "still streaming" in result

    def test_function_defined_in_streaming_py(self):
        src = read("api/streaming.py")
        assert "def _strip_xml_tool_calls(" in src, (
            "_strip_xml_tool_calls must be defined in api/streaming.py"
        )

    def test_strip_applied_to_assistant_messages(self):
        """Verify the strip call is applied to assistant message content after
        the agent run completes (server-side persistence fix)."""
        src = read("api/streaming.py")
        assert "_strip_xml_tool_calls" in src, (
            "_strip_xml_tool_calls must be referenced in api/streaming.py"
        )
        # Confirm it is called on message content, not just defined
        assert src.count("_strip_xml_tool_calls") >= 2, (
            "_strip_xml_tool_calls must be both defined and called"
        )

    def test_client_side_strip_in_messages_js(self):
        src = read("static/messages.js")
        assert "_stripXmlToolCalls" in src, (
            "Client-side _stripXmlToolCalls must exist in static/messages.js"
        )
        assert "function_calls" in src.lower(), (
            "Client-side strip must reference 'function_calls'"
        )

    def test_client_side_strip_in_ui_js(self):
        src = read("static/ui.js")
        assert "_stripXmlToolCallsDisplay" in src, (
            "_stripXmlToolCallsDisplay must exist in static/ui.js"
        )

    def test_thinking_card_text_is_sanitized(self):
        src = read("static/ui.js")
        assert "_sanitizeThinkingDisplayText" in src, (
            "Thinking card text sanitizer must exist in static/ui.js"
        )
        assert "_thinkingCardHtml" in src and "_thinkingMarkup" in src, (
            "Thinking card render helpers must exist in static/ui.js"
        )
        assert src.count("_sanitizeThinkingDisplayText(") >= 3, (
            "Thinking card helpers must call _sanitizeThinkingDisplayText"
        )


# ── Bug #703 — Workspace file panel empty state ───────────────────────────────


class TestWorkspaceEmptyState:
    def test_i18n_no_path_string_present(self):
        src = read("static/i18n.js")
        assert "workspace_empty_no_path" in src, (
            "i18n key workspace_empty_no_path must be defined in i18n.js"
        )

    def test_i18n_no_path_mentions_settings(self):
        src = read("static/i18n.js")
        # Extract the value of the key
        m = re.search(r"workspace_empty_no_path:\s*'([^']+)'", src)
        assert m, "workspace_empty_no_path value not found in i18n.js"
        assert "Settings" in m.group(1), (
            "workspace_empty_no_path should mention Settings"
        )

    def test_i18n_empty_dir_string_present(self):
        src = read("static/i18n.js")
        assert "workspace_empty_dir" in src, (
            "i18n key workspace_empty_dir must be defined in i18n.js"
        )

    def test_empty_state_element_in_html(self):
        src = read("static/index.html")
        assert "wsEmptyState" in src, (
            'id="wsEmptyState" empty-state element must exist in index.html'
        )

    def test_render_file_tree_shows_empty_state(self):
        src = read("static/ui.js")
        assert "wsEmptyState" in src, (
            "renderFileTree in ui.js must reference wsEmptyState"
        )
        assert "workspace_empty_no_path" in src, (
            "renderFileTree must use workspace_empty_no_path i18n key"
        )
        assert "workspace_empty_dir" in src, (
            "renderFileTree must use workspace_empty_dir i18n key"
        )


# ── Bug #704 — Notification description says "tab" ───────────────────────────


class TestNotificationDescriptionText:
    def test_english_uses_app_not_tab(self):
        src = read("static/i18n.js")
        # Find the English locale block (appears before other locales)
        # The English block starts at line 1 (it's the first locale object).
        # We look for the settings_desc_notifications in the English section.
        # English block ends before the Spanish (es) block.
        es_marker = "settings_desc_notifications: 'Muestra"
        en_end = src.index(es_marker) if es_marker in src else len(src)
        en_section = src[:en_end]

        m = re.search(r"settings_desc_notifications:\s*'([^']+)'", en_section)
        assert m, "English settings_desc_notifications not found"
        desc = m.group(1)
        assert "tab" not in desc.lower(), (
            f"English notification description must not say 'tab', got: {desc!r}"
        )
        assert "app" in desc.lower(), (
            f"English notification description must say 'app', got: {desc!r}"
        )

    def test_new_wording_exact(self):
        src = read("static/i18n.js")
        expected = "while the app is in the background"
        assert expected in src, f"Exact phrase {expected!r} must appear in i18n.js"

    def test_old_wording_removed_from_english(self):
        src = read("static/i18n.js")
        old_phrase = "while the tab is in the background"
        # The old phrase must not appear in the English locale section
        es_marker = "settings_desc_notifications: 'Muestra"
        en_end = src.index(es_marker) if es_marker in src else len(src)
        en_section = src[:en_end]
        assert old_phrase not in en_section, (
            "Old English notification description with 'tab' must be removed"
        )
