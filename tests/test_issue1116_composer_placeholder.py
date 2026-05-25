"""Tests for #1116 — composer placeholder reflects active profile name."""
import re


def _src(name: str) -> str:
    with open(f"static/{name}") as f:
        return f.read()


class TestComposerPlaceholderProfile:
    """applyBotName() should use the profile name when activeProfile is set."""

    def test_applyBotName_uses_profile_name(self):
        """Non-default profiles must use the profile name instead of bot_name."""
        src = _src("boot.js")
        ui_src = _src("ui.js")
        assert "function assistantDisplayName()" in ui_src, \
            "assistant display name resolution should be shared"
        assert "S.activeProfile&&S.activeProfile!=='default'" in ui_src, \
            "assistantDisplayName must only treat the literal default profile as renamed by bot_name"
        assert "assistantDisplayName()" in src, \
            "applyBotName must use the shared profile-aware display name"

    def test_applyBotName_capitalises_profile_name(self):
        """Profile name should be capitalised (first letter uppercase)."""
        src = _src("ui.js")
        m = re.search(r'function assistantDisplayName\(\)\{.*?\n\}', src, re.DOTALL)
        assert m, "assistantDisplayName function must exist"
        body = m.group(0)
        assert "charAt(0).toUpperCase()" in body, \
            "assistantDisplayName must capitalise first letter of profile name"

    def test_applyBotName_falls_back_to_bot_name(self):
        """The saved assistant name applies to the default profile."""
        src = _src("ui.js")
        m = re.search(r'function assistantDisplayName\(\)\{.*?\n\}', src, re.DOTALL)
        assert m, "assistantDisplayName function must exist"
        body = m.group(0)
        assert "window._botName||'Hermes'" in body, \
            "assistantDisplayName must use window._botName or 'Hermes' for the default profile"

    def test_chat_surfaces_use_shared_assistant_display_name(self):
        """Chat rows, titles, notifications, and cancel copy must honor profile overrides."""
        ui_src = _src("ui.js")
        messages_src = _src("messages.js")
        sessions_src = _src("sessions.js")
        assert "document.title=assistantDisplayName();" in ui_src
        assert "document.title=sessionTitle+' \\u2014 '+assistantDisplayName();" in ui_src
        assert "const _bn=assistantDisplayName();" in ui_src
        assert "assistantDisplayName()" in messages_src
        assert "assistantDisplayName()" in sessions_src

    def test_boot_applies_placeholder_after_active_profile_loads(self):
        """Boot must set the composer placeholder after S.activeProfile is known."""
        src = _src("boot.js")
        fetch_idx = src.find("api('/api/profile/active')")
        assert fetch_idx >= 0, "boot.js should fetch the active profile during boot"
        label_idx = src.find("const profileLabel=$('profileChipLabel');", fetch_idx)
        assert label_idx >= 0, "profile chip sync should follow active profile fetch"
        assert "applyBotName();" in src[fetch_idx:label_idx], (
            "boot should apply the profile-aware assistant name after active profile resolution"
        )

    def test_settings_copy_names_default_assistant_scope(self):
        """The preference copy must say that only the default profile is renamed."""
        index_src = _src("index.html")
        i18n_src = _src("i18n.js")
        assert "Default assistant name" in index_src
        assert "Used for the default profile only. Other profiles use their own profile names." in index_src
        assert "settings_label_bot_name: 'Default assistant name'" in i18n_src
        assert (
            "settings_desc_bot_name: 'Used for the default profile only. "
            "Other profiles use their own profile names.'"
        ) in i18n_src

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
