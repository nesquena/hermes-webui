"""Tests for #1116 — composer placeholder reflects active profile name."""
import json
import pathlib
import re
import subprocess
import textwrap

REPO_ROOT = pathlib.Path(__file__).resolve().parent.parent


def _src(name: str) -> str:
    with open(f"static/{name}") as f:
        return f.read()


def _resolve_en(key: str, *args) -> str:
    """Resolve an i18n key through the REAL `t()` in static/i18n.js (node vm).

    Used instead of a source regex so the assertion pins the English text a
    user actually sees, not the shape of the call site.
    """
    expr = "t(" + ", ".join([json.dumps(key), *(json.dumps(a) for a in args)]) + ")"
    script = textwrap.dedent(
        f"""
        const fs = require('fs');
        const vm = require('vm');
        const src = fs.readFileSync({json.dumps(str(REPO_ROOT / 'static' / 'i18n.js'))}, 'utf8');
        const storage = {{}};
        const ctx = {{
          localStorage: {{
            getItem: (k) => Object.prototype.hasOwnProperty.call(storage, k) ? storage[k] : null,
            setItem: (k, v) => {{ storage[k] = String(v); }},
          }},
          document: {{ documentElement: {{ lang: '' }}, querySelectorAll: () => [] }},
          navigator: undefined,
        }};
        vm.createContext(ctx);
        vm.runInContext(src, ctx);
        process.stdout.write(JSON.stringify(vm.runInContext({json.dumps(expr)}, ctx)));
        """
    )
    proc = subprocess.run(
        ["node", "-e", script], check=True, capture_output=True, text=True
    )
    return json.loads(proc.stdout)


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
        fetch_idx = src.find("const activeProfileState = await _resolveActiveProfileBootstrapState();")
        assert fetch_idx >= 0, "boot.js should resolve the active profile during boot"
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
        # Slice the WHOLE function body — bounded by the next top-level function
        # (openProfileCreate) — rather than a fixed char window. The profile-switch
        # loading-skeleton + race-guard work (#4671) grew switchToProfile past every
        # fixed window we tried (5000 -> 6500 -> still short at offset ~6609), so anchor
        # on the next-function boundary instead so this can't drift again. The
        # applyBotName() call still fires on every switch; this is purely about the test
        # reading the whole body.
        end = src.find("function openProfileCreate(", m.start())
        after = src[m.start():end] if end != -1 else src[m.start():]
        assert "applyBotName" in after, \
            "switchToProfile must call applyBotName after profile switch"

    def test_placeholder_uses_name_variable(self):
        """The composer placeholder must use the resolved name variable.

        #7697 moved the literal out of boot.js: applyBotName now passes the
        resolved name into the `composer_placeholder_idle` catalog key. The
        source therefore no longer contains the word 'Message', but the
        ENGLISH output must still resolve to 'Message <name>…' — asserted
        below against the real `t()` in static/i18n.js, not a copy of it.
        """
        src = _src("boot.js")
        m = re.search(r'function applyBotName\(\)\{.*?\n\}', src, re.DOTALL)
        assert m, "applyBotName function must exist"
        body = m.group(0)
        call = re.search(
            r"msg\.placeholder\s*=\s*t\(\s*'([^']+)'\s*,\s*name\s*\)", body
        )
        assert call, (
            "applyBotName must set the composer placeholder from an i18n key, "
            "passing the resolved name as the {0} argument "
            "(e.g. t('composer_placeholder_idle', name))"
        )
        # Resolve the key through the production i18n runtime so a wrong/renamed
        # catalog value cannot slip past a source-only assertion.
        assert _resolve_en(call.group(1), "Hermes") == "Message Hermes…"
