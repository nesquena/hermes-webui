"""Tests for /reasoning show|hide slash command and show_thinking setting.

Covers:
  - show_thinking in _SETTINGS_DEFAULTS and _SETTINGS_BOOL_KEYS (api/config.py)
  - window._showThinking initialised in boot.js (settings and fallback paths)
  - window._showThinking guard in ui.js renderMessages thinking card
  - _renderLiveThinking guard in messages.js
  - cmdReasoning function present in commands.js with show/hide/effort handling
  - /reasoning in COMMANDS array (not just SLASH_SUBARG_SOURCES)
  - show|hide present as subArgs in COMMANDS entry
"""

import pathlib
import re

REPO = pathlib.Path(__file__).parent.parent


def read(rel):
    return (REPO / rel).read_text(encoding='utf-8')


# ── api/config.py ─────────────────────────────────────────────────────────────

class TestShowThinkingConfig:
    """show_thinking must appear in defaults and bool keys."""

    def test_show_thinking_in_defaults(self):
        src = read('api/config.py')
        assert '"show_thinking": True' in src, (
            "show_thinking must be True in _SETTINGS_DEFAULTS"
        )

    def test_show_thinking_in_bool_keys(self):
        src = read('api/config.py')
        assert '"show_thinking"' in src
        # Find the _SETTINGS_BOOL_KEYS set and confirm show_thinking is in it
        m = re.search(r'_SETTINGS_BOOL_KEYS\s*=\s*\{([^}]+)\}', src, re.DOTALL)
        assert m, "_SETTINGS_BOOL_KEYS not found"
        assert 'show_thinking' in m.group(1), (
            "show_thinking must be in _SETTINGS_BOOL_KEYS"
        )


# ── static/boot.js ────────────────────────────────────────────────────────────

class TestBootJsShowThinking:
    """window._showThinking must be set in both the settings and fallback paths."""

    def test_settings_path_initialises_show_thinking(self):
        src = read('static/boot.js')
        # Must read from the settings object, defaulting true when absent
        assert 'window._showThinking=s.show_thinking!==false' in src, (
            "boot.js must initialise _showThinking from settings (default true)"
        )

    def test_fallback_path_initialises_show_thinking_true(self):
        src = read('static/boot.js')
        assert 'window._showThinking=true' in src, (
            "boot.js fallback path must default _showThinking to true"
        )


# ── static/ui.js ──────────────────────────────────────────────────────────────

class TestUiJsThinkingGate:
    """Historical thinking cards must be gated by window._showThinking."""

    def test_thinking_card_gated_in_render_messages(self):
        src = read('static/ui.js')
        assert 'window._showThinking!==false' in src, (
            "ui.js must gate thinkingCardHtml on window._showThinking"
        )
        # The guard must be on the same line as _thinkingCardHtml insertion
        lines = src.splitlines()
        for line in lines:
            if '_thinkingCardHtml' in line and 'insertAdjacentHTML' in line:
                assert 'window._showThinking' in line, (
                    f"thinking card insertion must be gated: {line.strip()}"
                )
                break


# ── static/messages.js ────────────────────────────────────────────────────────

class TestMessagesJsLiveThinkingGate:
    """Live streaming thinking card must be hidden when _showThinking is false."""

    def test_live_thinking_gated(self):
        src = read('static/messages.js')
        assert 'window._showThinking===false' in src, (
            "messages.js _renderLiveThinking must early-return when _showThinking is false"
        )
        # Guard must be inside _renderLiveThinking
        m = re.search(r'function _renderLiveThinking\(.*?\{(.*?)^\s*\}',
                      src, re.DOTALL | re.MULTILINE)
        assert m, "_renderLiveThinking not found"
        assert 'window._showThinking' in m.group(1)


# ── static/commands.js ────────────────────────────────────────────────────────

class TestReasoningCommand:
    """cmdReasoning must be wired into COMMANDS with show/hide subArgs."""

    def test_reasoning_in_commands_array(self):
        src = read('static/commands.js')
        # Must appear in COMMANDS array (not just SLASH_SUBARG_SOURCES)
        m = re.search(r'const COMMANDS\s*=\s*\[(.*?)\];', src, re.DOTALL)
        assert m, "COMMANDS array not found"
        commands_block = m.group(1)
        assert 'reasoning' in commands_block, (
            "/reasoning must be in the COMMANDS array with a fn: handler"
        )
        assert 'fn:cmdReasoning' in commands_block or "fn: cmdReasoning" in commands_block, (
            "/reasoning entry must reference cmdReasoning"
        )

    def test_reasoning_subargs_include_show_hide(self):
        src = read('static/commands.js')
        m = re.search(r'const COMMANDS\s*=\s*\[(.*?)\];', src, re.DOTALL)
        assert m
        commands_block = m.group(1)
        # Find the reasoning entry
        rm = re.search(r"\{name:'reasoning'.*?\}", commands_block, re.DOTALL)
        assert rm, "reasoning entry not found in COMMANDS"
        entry = rm.group(0)
        assert 'show' in entry, "subArgs must include 'show'"
        assert 'hide' in entry, "subArgs must include 'hide'"

    def test_reasoning_not_only_in_subarg_sources(self):
        src = read('static/commands.js')
        # It's fine if SLASH_SUBARG_SOURCES is empty or doesn't have reasoning
        # (reasoning moved to COMMANDS with a real fn)
        m = re.search(r'const SLASH_SUBARG_SOURCES\s*=\s*\{(.*?)\};', src, re.DOTALL)
        if m:
            subarg_block = m.group(1)
            assert 'reasoning' not in subarg_block, (
                "reasoning must not remain in SLASH_SUBARG_SOURCES once it has a fn: handler"
            )

    def test_cmd_reasoning_function_exists(self):
        src = read('static/commands.js')
        assert 'function cmdReasoning' in src, (
            "cmdReasoning function must be defined"
        )

    def test_cmd_reasoning_handles_show(self):
        src = read('static/commands.js')
        m = re.search(r'function cmdReasoning\(.*?\n\}', src, re.DOTALL)
        assert m, "cmdReasoning not found"
        fn = m.group(0)
        assert '_showThinking=true' in fn or '_showThinking = true' in fn, (
            "cmdReasoning show branch must set _showThinking=true"
        )
        assert 'renderMessages' in fn, (
            "cmdReasoning show branch must call renderMessages()"
        )
        assert 'show_thinking' in fn, (
            "cmdReasoning show branch must persist via /api/settings"
        )

    def test_cmd_reasoning_handles_hide(self):
        src = read('static/commands.js')
        m = re.search(r'function cmdReasoning\(.*?\n\}', src, re.DOTALL)
        assert m
        fn = m.group(0)
        assert '_showThinking=false' in fn or '_showThinking = false' in fn, (
            "cmdReasoning hide branch must set _showThinking=false"
        )

    def test_cmd_reasoning_i18n_key_exists(self):
        i18n = read('static/i18n.js')
        assert 'cmd_reasoning' in i18n, (
            "i18n.js must define the cmd_reasoning key"
        )

    def test_cmd_reasoning_posts_settings_not_gets(self):
        """Regression: the api() helper spreads its 2nd arg into fetch(), so
        passing `{show_thinking:true}` alone would silently become a GET —
        the persistence would never happen. Every /api/settings call inside
        cmdReasoning must explicitly use method:'POST' + JSON body."""
        src = read('static/commands.js')
        m = re.search(r'function cmdReasoning\(.*?\n\}', src, re.DOTALL)
        assert m
        fn = m.group(0)
        api_calls = re.findall(r"api\('/api/settings'[^)]*\)", fn)
        assert api_calls, "cmdReasoning must call /api/settings at least once"
        for call in api_calls:
            assert "method:'POST'" in call or 'method: "POST"' in call, (
                f"/api/settings call missing method:'POST' — would fall "
                f"through to GET and silently drop the update: {call}"
            )
            assert 'JSON.stringify' in call, (
                f"/api/settings call missing JSON body: {call}"
            )
