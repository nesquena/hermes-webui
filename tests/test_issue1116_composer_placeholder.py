"""Tests for #1116 — composer placeholder reflects active profile name."""
import re


def _src(name: str) -> str:
    with open(f"static/{name}") as f:
        return f.read()


class TestComposerPlaceholderProfile:
    """applyBotName() should use the profile name when activeProfile is set."""

    def test_applyBotName_uses_profile_name(self):
        """applyBotName must check S.activeProfile and prefer it over global bot_name."""
        src = _src("boot.js")
        assert "S.activeProfile" in src, \
            "applyBotName must reference S.activeProfile"
        # Should fall back to _botName when activeProfile is 'default'
        assert "S.activeProfile!=='default'" in src, \
            "applyBotName must skip 'default' profile (use bot_name instead)"

    def test_applyBotName_capitalises_profile_name(self):
        """Profile name should be capitalised (first letter uppercase)."""
        src = _src("boot.js")
        m = re.search(r'function applyBotName\(\)\{.*?\n\}', src, re.DOTALL)
        assert m, "applyBotName function must exist"
        body = m.group(0)
        assert "charAt(0).toUpperCase()" in body, \
            "applyBotName must capitalise first letter of profile name"

    def test_applyBotName_falls_back_to_bot_name(self):
        """When no active profile, must fall back to window._botName."""
        src = _src("boot.js")
        m = re.search(r'function applyBotName\(\)\{.*?\n\}', src, re.DOTALL)
        assert m, "applyBotName function must exist"
        body = m.group(0)
        assert "window._botName||'Hermes'" in body, \
            "applyBotName must fall back to window._botName or 'Hermes'"

    def test_switchToProfile_calls_applyBotName(self):
        """switchToProfile() must call applyBotName() after switching."""
        src = _src("panels.js")
        assert "function switchToProfile" in src, \
            "switchToProfile function must exist"
        # Find the function block (starts with 'async function switchToProfile')
        m = re.search(r'async function switchToProfile\s*\(', src)
        assert m, "switchToProfile must be an async function"
        # Get everything after the function declaration (enough context)
        after = src[m.start():m.start()+5000]
        assert "applyBotName" in after, \
            "switchToProfile must call applyBotName after profile switch"

    def test_placeholder_uses_name_variable(self):
        """The composer placeholder must use the resolved name variable."""
        src = _src("boot.js")
        m = re.search(r'function applyBotName\(\)\{.*?\n\}', src, re.DOTALL)
        assert m, "applyBotName function must exist"
        body = m.group(0)
        assert re.search(r"msg\.placeholder\s*=\s*.*Message.*name", body), \
            "applyBotName must set composer placeholder to 'Message <name>…'"
