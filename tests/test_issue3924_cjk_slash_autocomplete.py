"""Tests for #3924 — CJK text before / should not block slash autocomplete.

The autocomplete trigger previously gated on text.startsWith('/') which
fails when CJK (or any non-/ text) precedes the slash. The fix introduces
_activeSlashCommandOffset() which finds the / at a token-initial position
(start of line or after whitespace), excluding ~/ path tokens and mid-word
slashes (URLs, provider/model IDs).
"""
import os
import re


_SRC = os.path.join(os.path.dirname(__file__), "..")


def _read(name):
    return open(os.path.join(_SRC, name), encoding="utf-8").read()


class TestActiveSlashCommandOffset:
    """_activeSlashCommandOffset must exist and use token-initial logic."""

    def test_helper_exists(self):
        js = _read("static/commands.js")
        assert "function _activeSlashCommandOffset(text)" in js

    def test_helper_checks_token_initial_position(self):
        js = _read("static/commands.js")
        m = re.search(
            r'function _activeSlashCommandOffset\(text\)\{(.*?)\n\}',
            js, re.DOTALL,
        )
        assert m, "_activeSlashCommandOffset not found"
        fn = m.group(0)
        # Must check for start-of-line (i===0) or after whitespace
        assert "i===0" in fn or "i === 0" in fn, (
            "_activeSlashCommandOffset must check start-of-line position"
        )
        assert "text[i-1]" in fn, (
            "_activeSlashCommandOffset must check character before /"
        )

    def test_helper_excludes_path_tokens(self):
        js = _read("static/commands.js")
        m = re.search(
            r'function _activeSlashCommandOffset\(text\)\{(.*?)\n\}',
            js, re.DOTALL,
        )
        assert m
        fn = m.group(0)
        # Must exclude ~/ path tokens
        assert "~" in fn, (
            "_activeSlashCommandOffset must exclude ~/ path tokens"
        )


class TestParseSlashAutocompleteUsesHelper:
    """_parseSlashAutocomplete must delegate to _activeSlashCommandOffset."""

    def test_delegates_to_helper(self):
        js = _read("static/commands.js")
        m = re.search(
            r'function _parseSlashAutocomplete\(text\)\{(.*?)\n\}',
            js, re.DOTALL,
        )
        assert m, "_parseSlashAutocomplete not found"
        fn = m.group(0)
        assert "_activeSlashCommandOffset" in fn, (
            "_parseSlashAutocomplete must use _activeSlashCommandOffset"
        )

    def test_does_not_use_startswith(self):
        js = _read("static/commands.js")
        m = re.search(
            r'function _parseSlashAutocomplete\(text\)\{(.*?)\n\}',
            js, re.DOTALL,
        )
        assert m
        fn = m.group(0)
        assert "startsWith('/')" not in fn, (
            "_parseSlashAutocomplete must not gate on startsWith('/')"
        )


class TestRefreshSlashCommandDropdownUsesHelper:
    """refreshSlashCommandDropdown must delegate to _activeSlashCommandOffset."""

    def test_delegates_to_helper(self):
        js = _read("static/commands.js")
        m = re.search(
            r'function refreshSlashCommandDropdown\(\)\{(.*?)\n\}',
            js, re.DOTALL,
        )
        assert m, "refreshSlashCommandDropdown not found"
        fn = m.group(0)
        assert "_activeSlashCommandOffset" in fn, (
            "refreshSlashCommandDropdown must use _activeSlashCommandOffset"
        )


class TestBootJsSlashTrigger:
    """boot.js input handler must use _activeSlashCommandOffset."""

    def test_boot_uses_helper(self):
        js = _read("static/boot.js")
        assert "_activeSlashCommandOffset" in js, (
            "boot.js must use _activeSlashCommandOffset for the slash "
            "autocomplete trigger (#3924)"
        )

    def test_boot_falls_back_to_path_autocomplete(self):
        js = _read("static/boot.js")
        # The path autocomplete else-if must still be present
        assert "getComposerPathAutocompleteMatches" in js, (
            "boot.js must preserve path autocomplete fallback"
        )


class TestSelectionHandlerPreservesPrefix:
    """When an autocomplete item is selected, prefix text before the active
    slash must be preserved rather than discarded."""

    def test_selection_uses_helper(self):
        js = _read("static/commands.js")
        m = re.search(
            r'function showCmdDropdown\(matches\)\{(.*?)\nfunction hideCmdDropdown',
            js, re.DOTALL,
        )
        assert m, "showCmdDropdown not found"
        fn = m.group(0)
        assert "_activeSlashCommandOffset" in fn, (
            "showCmdDropdown selection handler must use _activeSlashCommandOffset "
            "to preserve prefix text before the slash (#3924)"
        )
