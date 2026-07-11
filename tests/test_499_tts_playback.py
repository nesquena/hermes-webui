"""
Tests for #499: TTS playback of agent responses via Web Speech API.

Verifies that TTS utility functions, speaker button rendering, and
settings controls are present in the WebUI codebase.
"""
import os
import re

STATIC_DIR = os.path.join(os.path.dirname(__file__), '..', 'static')
REPO_DIR = os.path.join(os.path.dirname(__file__), '..')


def _read(filename):
    return open(os.path.join(STATIC_DIR, filename), encoding='utf-8').read()


def _read_repo(relative_path):
    """Read a file from the repo root (e.g. 'api/routes.py')."""
    return open(os.path.join(REPO_DIR, relative_path), encoding='utf-8').read()


class TestTtsUtilityFunctions:
    """TTS core functions exist in ui.js."""

    def test_strip_for_tts_exists(self):
        src = _read('ui.js')
        assert 'function _stripForTTS(' in src, \
            "_stripForTTS function not found in ui.js"

    def test_speak_message_exists(self):
        src = _read('ui.js')
        assert 'function speakMessage(' in src, \
            "speakMessage function not found in ui.js"

    def test_stop_tts_exists(self):
        src = _read('ui.js')
        assert 'function stopTTS(' in src, \
            "stopTTS function not found in ui.js"

    def test_auto_read_exists(self):
        src = _read('ui.js')
        assert 'function autoReadLastAssistant(' in src, \
            "autoReadLastAssistant function not found in ui.js"

    def test_strip_code_blocks(self):
        """_stripForTTS must remove ``` code blocks."""
        src = _read('ui.js')
        assert re.search(r'_stripForTTS.*```', src, re.DOTALL), \
            "_stripForTTS must handle fenced code blocks"

    def test_strip_media_paths(self):
        """_stripForTTS must replace MEDIA: paths."""
        src = _read('ui.js')
        assert 'MEDIA:' in src and 'a file' in src, \
            "_stripForTTS must replace MEDIA: paths"

    def test_uses_speech_synthesis(self):
        """speakMessage must use window.speechSynthesis."""
        src = _read('ui.js')
        assert 'SpeechSynthesisUtterance' in src, \
            "speakMessage must create SpeechSynthesisUtterance"
        assert 'speechSynthesis.speak' in src, \
            "speakMessage must call speechSynthesis.speak"

    def test_play_server_tts_exists(self):
        """_playServerTts unified server TTS function exists."""
        src = _read('ui.js')
        assert 'function _playServerTts(' in src, \
            "_playServerTts function not found in ui.js"

    def test_no_elevenlabs_engine_dispatch(self):
        """speakMessage no longer dispatches to elevenlabs engine."""
        src = _read('ui.js')
        # The old pattern: if(engine==='elevenlabs'){_playElevenLabsTts(...)
        assert "engine==='elevenlabs'" not in src, \
            "Old elevenlabs engine dispatch should be removed from speakMessage"

    def test_no_edge_engine_dispatch(self):
        """speakMessage no longer dispatches to edge engine."""
        src = _read('ui.js')
        assert "engine==='edge'" not in src, \
            "Old edge engine dispatch should be removed from speakMessage"


class TestTtsSpeakerButton:
    """Speaker button is rendered on assistant messages."""

    def test_tts_button_rendered(self):
        """ttsBtn must be generated for non-user messages."""
        src = _read('ui.js')
        assert 'msg-tts-btn' in src, \
            "TTS button class not found in ui.js"

    def test_tts_button_not_on_user_messages(self):
        """ttsBtn must only be added for non-user (assistant) messages."""
        src = _read('ui.js')
        # Find the ttsBtn definition — it should have !isUser guard
        tts_line = [l for l in src.splitlines() if 'msg-tts-btn' in l][0]
        assert '!isUser' in tts_line or 'isUser' in tts_line, \
            "TTS button should have user-check guard"

    def test_tts_button_in_footer(self):
        """ttsBtn must be included in the msg-actions span."""
        src = _read('ui.js')
        # The footHtml line should include ttsBtn
        foot_lines = [l for l in src.splitlines() if 'footHtml' in l and 'msg-actions' in l]
        assert any('ttsBtn' in l for l in foot_lines), \
            "ttsBtn not included in footHtml msg-actions"

    def test_tts_button_uses_volume_icon(self):
        """Speaker button should use volume-2 icon."""
        src = _read('ui.js')
        tts_line = [l for l in src.splitlines() if 'msg-tts-btn' in l][0]
        assert 'volume-2' in tts_line, \
            "TTS button should use volume-2 icon"


class TestTtsSettings:
    """TTS settings controls exist in the HTML and are wired in panels.js."""

    def test_tts_enabled_checkbox(self):
        src = _read('index.html')
        assert 'settingsTtsEnabled' in src, \
            "TTS enabled checkbox not found in index.html"

    def test_tts_auto_read_checkbox(self):
        src = _read('index.html')
        assert 'settingsTtsAutoRead' in src, \
            "TTS auto-read checkbox not found in index.html"

    def test_tts_voice_selector(self):
        src = _read('index.html')
        assert 'settingsTtsVoice' in src, \
            "TTS voice selector not found in index.html"

    def test_tts_engine_is_capability_driven(self):
        src = _read('index.html')
        assert '<option value="browser">Browser speech synthesis</option>' in src, \
            "settingsTtsEngine must keep the browser-local option"
        assert '<option value="openai">OpenAI TTS (server)</option>' not in src, \
            "Server TTS providers must be populated from /api/tts/capability, not hardcoded"

    def test_tts_rate_slider(self):
        src = _read('index.html')
        assert 'settingsTtsRate' in src, \
            "TTS rate slider not found in index.html"

    def test_tts_pitch_slider(self):
        src = _read('index.html')
        assert 'settingsTtsPitch' in src, \
            "TTS pitch slider not found in index.html"

    def test_tts_settings_wired_in_panels(self):
        """TTS settings must be initialized in loadSettingsPanel."""
        src = _read('panels.js')
        assert 'settingsTtsEnabled' in src, \
            "TTS enabled setting not wired in panels.js"
        assert '_applyTtsEnabled' in src, \
            "_applyTtsEnabled not called in panels.js"

    def test_apply_tts_enabled_function(self):
        """_applyTtsEnabled must toggle msg-tts-btn display."""
        src = _read('panels.js')
        assert 'function _applyTtsEnabled(' in src, \
            "_applyTtsEnabled function not found in panels.js"

    def test_server_voice_placeholder_in_panels(self):
        src = _read('panels.js')
        assert "engine==='server'||engine.startsWith('server:')" in src, \
            "panels.js must treat all server providers as agent-configured TTS"
        assert 'Server-configured voice' in src, \
            "Server TTS providers must present a server-configured voice placeholder"


class TestTtsI18n:
    """TTS i18n keys exist in the English locale."""

    def test_tts_listen_key(self):
        src = _read('i18n.js')
        assert "tts_listen:" in src, \
            "tts_listen key not found in i18n.js"

    def test_tts_not_supported_key(self):
        src = _read('i18n.js')
        assert "tts_not_supported:" in src, \
            "tts_not_supported key not found in i18n.js"

    def test_tts_settings_keys(self):
        src = _read('i18n.js')
        for key in ['settings_label_tts', 'settings_label_tts_auto_read',
                     'settings_label_tts_voice', 'settings_label_tts_rate',
                     'settings_label_tts_pitch']:
            assert f"{key}:" in src, f"{key} not found in i18n.js"


class TestTtsAutoRead:
    """Auto-read is triggered after SSE done event."""

    def test_auto_read_called_in_messages(self):
        src = _read('messages.js')
        assert 'autoReadLastAssistant' in src, \
            "autoReadLastAssistant not called in messages.js"

    def test_tts_pause_on_composer_focus(self):
        """Speech should pause when user focuses the composer."""
        src = _read('messages.js')
        assert 'speechSynthesis.pause' in src, \
            "speechSynthesis.pause not called in messages.js"
        assert 'speechSynthesis.resume' in src, \
            "speechSynthesis.resume not called in messages.js"


class TestTtsBoot:
    """TTS enabled state is applied on page load."""

    def test_apply_tts_on_boot(self):
        src = _read('boot.js')
        assert '_applyTtsEnabled' in src, \
            "_applyTtsEnabled not called in boot.js"


class TestTtsStyles:
    """TTS CSS styles exist."""

    def test_tts_button_hidden_default(self):
        src = _read('style.css')
        assert '.msg-tts-btn' in src, \
            ".msg-tts-btn CSS class not found in style.css"

    def test_tts_pulse_animation(self):
        src = _read('style.css')
        assert 'tts-pulse' in src, \
            "tts-pulse animation not found in style.css"


class TestIssue1409TtsToggleBodyClass:
    """Regression: #1409 — TTS toggle had no effect because of CSS specificity collision.

    Original bug: ``_applyTtsEnabled`` set ``btn.style.display=enabled?'':'none'``.
    The empty-string branch removes the inline override, after which the
    ``.msg-tts-btn { display:none; }`` rule from style.css applies — so both
    "enabled" and "disabled" states left the button hidden.

    Fix: toggle a body-level class (``body.tts-enabled``) and gate the speaker
    icon on a compound selector ``body.tts-enabled .msg-tts-btn``. This bypasses
    the inline-style cascade collision and survives ``renderMd()`` re-renders.
    """

    def test_apply_tts_enabled_uses_body_class(self):
        """_applyTtsEnabled must toggle the document body's `tts-enabled` class."""
        src = _read('panels.js')
        # The new shape: toggle body class instead of writing inline display
        assert "document.body.classList.toggle('tts-enabled'" in src, (
            "_applyTtsEnabled must toggle the body.tts-enabled class — see #1409. "
            "Reverting to inline `style.display` will silently break the toggle "
            "again because of the .msg-action-btn / .msg-tts-btn cascade."
        )

    def test_apply_tts_enabled_does_not_use_inline_display(self):
        """_applyTtsEnabled must NOT set inline `style.display` on .msg-tts-btn."""
        src = _read('panels.js')
        # Find the function body and check it doesn't set inline display
        # on individual buttons (the broken pattern).
        m = re.search(
            r'function _applyTtsEnabled\([^)]*\)\s*\{(?P<body>[^}]*)\}',
            src,
        )
        assert m, "_applyTtsEnabled function body not found in panels.js"
        body = m.group('body')
        assert '.style.display' not in body, (
            "_applyTtsEnabled body must not set inline style.display — that's "
            "the #1409 bug. Use body.classList.toggle('tts-enabled') instead."
        )

    def test_body_class_selector_in_css(self):
        """style.css must show .msg-tts-btn only when body.tts-enabled is set."""
        src = _read('style.css')
        assert 'body.tts-enabled .msg-tts-btn' in src, (
            "Missing `body.tts-enabled .msg-tts-btn` selector in style.css — "
            "without this rule the body class has no visual effect (#1409)."
        )
        # The default-hidden rule must still be present (so no body class = no icon).
        assert '.msg-tts-btn{display:none;}' in src or \
               re.search(r'\.msg-tts-btn\s*\{[^}]*display\s*:\s*none', src), (
            "Default `.msg-tts-btn{display:none;}` rule must remain so the "
            "icon is hidden by default (#1409)."
        )


class TestCustomProviderInjection:
    """Custom command providers and plugin providers in config.yaml must
    surface in the WebUI dropdown — not silently fall back to browser.
    """

    def test_synthetic_option_code_present(self):
        """panels.js must inject a synthetic option for unknown config providers."""
        src = _read('panels.js')
        # The JS template is: configProvider + ' (from config.yaml)'
        # so the literal string '(from config.yaml)' is in the source.
        assert '(from config.yaml)' in src, (
            "Missing synthetic option label for unknown TTS providers — "
            "custom command and plugin providers would be hidden from the "
            "user otherwise."
        )

    def test_synthetic_option_block_uses_cap_provider(self):
        """The synthetic-option block must reference cap.provider (not a stale variable)."""
        src = _read('panels.js')
        # The block that injects the synthetic option should:
        # 1. Read cap.provider
        # 2. Check against the allowlist
        # 3. Append a new <option> if not in the allowlist
        assert "cap.provider" in src, (
            "Synthetic option code must read cap.provider from the "
            "capability response"
        )
        assert "allowlistHasProvider" in src, (
            "Missing allowlist check for synthetic option injection"
        )

    def test_configProvider_declared_before_use(self):
        """The configProvider variable must be declared before any reference to it.

        Regression test for the ReferenceError bug where the synthetic
        option block referenced configProvider before declaring it.
        """
        src = _read('panels.js')
        # Find the synthetic option block
        block_match = re.search(
            r'// Inject the config provider.*?const configOption',
            src, re.DOTALL
        )
        assert block_match, "Synthetic option block not found"
        block = block_match.group(0)
        decl_pos = block.find("const configProvider")
        first_use_pos = block.find("configProvider", decl_pos + 1) if decl_pos >= 0 else -1
        assert decl_pos >= 0, (
            "const configProvider declaration missing in synthetic option block"
        )
        assert first_use_pos > decl_pos, (
            f"configProvider used at offset {first_use_pos} before declaration "
            f"at offset {decl_pos} — causes ReferenceError"
        )

    def test_synthetic_option_value_format(self):
        """The synthetic option must use the 'server:<provider>' value format."""
        src = _read('panels.js')
        # The synthetic option should be added with value 'server:' + configProvider
        assert "opt.value='server:' + configProvider" in src, (
            "Synthetic option value must use 'server:<provider>' format to "
            "match the existing dropdown values"
        )


class TestTtsEdgeCases:
    """Edge cases for the TTS subsystem — bugs caught during testing."""

    def test_tts_voice_public_wrapper_does_not_recurse(self):
        """Regression: window._populateTtsVoices must not call itself forever."""
        src = _read('panels.js')
        assert 'function _renderTtsVoiceOptions(' in src
        assert 'const renderVoices=()=>_renderTtsVoiceOptions(ttsVoiceSel,speechSetting);' in src
        assert 'const renderVoices=()=>_populateTtsVoices(ttsVoiceSel,speechSetting);' not in src, (
            "renderVoices calling _populateTtsVoices recurses after assigning "
            "window._populateTtsVoices=renderVoices"
        )

    def test_server_voice_dropdown_not_gated_on_browser_speech_synthesis(self):
        """Edge/server voice options must render even if browser speech synthesis is absent."""
        src = _read('panels.js')
        assert "if(ttsVoiceSel){" in src
        assert "if(ttsVoiceSel&&'speechSynthesis' in window)" not in src, (
            "The Edge/server voice dropdown must not be hidden behind the "
            "browser speechSynthesis availability check"
        )

    def test_no_direct_edge_tts_communicate(self):
        """No direct edge_tts.Communicate call — all providers must delegate to agent."""
        src = _read('ui.js')  # also check ui.js in case
        assert 'edge_tts.Communicate' not in src, (
            "ui.js must not call edge_tts.Communicate directly — "
            "Edge TTS should delegate to agent's text_to_speech_tool"
        )
        # Check boot.js too
        boot_src = _read('boot.js')
        assert 'edge_tts.Communicate' not in boot_src, (
            "boot.js must not call edge_tts.Communicate directly"
        )

    def test_no_elevenlabs_urlopen(self):
        """No direct ElevenLabs HTTP call — must delegate to agent."""
        for filename in ('ui.js', 'boot.js', 'panels.js'):
            src = _read(filename)
            assert 'elevenlabs' not in src.lower() or 'label' in src.lower() or 'label:' in src.lower() or 'agent' in src.lower() or 'elevenlabs:' in src.lower(), (
                f"{filename} must not have a direct ElevenLabs integration"
            )

    def test_no_normalize_tts_prosody(self):
        """The _normalize_tts_prosody function must be removed (rate/pitch are client-side)."""
        api_routes = _read_repo('api/routes.py')
        assert '_normalize_tts_prosody' not in api_routes, (
            "_normalize_tts_prosody is dead code — rate/pitch are applied "
            "client-side via playbackRate. Remove it."
        )

    def test_capability_endpoint_requires_auth_when_auth_enabled(self):
        """The /api/tts/capability endpoint must not bypass auth."""
        auth_path = os.path.join(REPO_DIR, 'api', 'auth.py')
        if not os.path.exists(auth_path):
            return  # test skipped if auth.py not present
        auth_src = open(auth_path).read()
        public_idx = auth_src.find('PUBLIC_PATHS')
        assert public_idx != -1, "auth.py must define PUBLIC_PATHS"
        public_block = auth_src[public_idx:auth_src.find('})', public_idx)]
        assert '/api/tts/capability' not in public_block, (
            "/api/tts/capability leaks provider/config/credential availability "
            "and must stay behind normal WebUI auth"
        )

    def test_no_multilingual_voices_in_dropdown(self):
        """Multilingual voices must not be in the dropdown (they hang on edge_tts)."""
        src = _read('panels.js')
        # Find the edgeVoices array and check it doesn't contain
        # Multilingual voices. The string may appear in comments — we
        # only care about the actual voice entries.
        import re
        match = re.search(r"const edgeVoices=\[(.*?)\];", src, re.DOTALL)
        assert match, "edgeVoices array not found"
        block = match.group(1)
        # Multilingual voices are named like en-US-JennyMultilingualNeural
        assert 'MultilingualNeural' not in block, (
            "Multilingual voices (e.g. en-US-JennyMultilingualNeural) hang the "
            "edge_tts library indefinitely. They must not be in the dropdown."
        )

    def test_pitch_description_no_false_config_claim(self):
        """Speech pitch description must not claim a config.yaml setting that doesn't exist."""
        src = _read('index.html')
        # The old description said "Server TTS pitch is configured in config.yaml"
        # which is false — the agent has no general tts.pitch setting.
        assert 'Server TTS pitch is configured in config.yaml' not in src, (
            "Pitch description must not claim a non-existent config.yaml setting"
        )

    def test_aia_default_voice_in_dropdown(self):
        """en-US-AriaNeural (the agent's DEFAULT_EDGE_VOICE) must be in the dropdown."""
        src = _read('panels.js')
        assert "'en-US-AriaNeural'" in src, (
            "en-US-AriaNeural is the agent's DEFAULT_EDGE_VOICE — must be in "
            "the dropdown so users with no tts.edge.voice configured still "
            "see a recognizable name"
        )

    def test_idonesian_voice_preserved(self):
        """The Indonesian voice (contributor's work) must be preserved in the dropdown."""
        src = _read('panels.js')
        assert "'id-ID-GadisNeural'" in src, (
            "id-ID-GadisNeural was added 2 days ago by a contributor — must "
            "be preserved in the dropdown."
        )

    def test_voice_count_in_range(self):
        """Voice dropdown should have 8-13 voices (per design)."""
        src = _read('panels.js')
        # Count voice entries in the edgeVoices array
        match = re.search(r"const edgeVoices=\[(.*?)\];", src, re.DOTALL)
        assert match, "edgeVoices array not found"
        block = match.group(1)
        count = len(re.findall(r"\{value:'", block))
        assert 8 <= count <= 15, (
            f"Voice dropdown has {count} entries — expected 8-15 "
            f"(current target is ~10-11)"
        )


class TestTtsSettingsDescriptions:
    """Settings panel descriptions must be accurate and not reference non-existent config."""

    def test_tts_engine_description_present(self):
        """TTS Engine description must be set."""
        src = _read('index.html')
        assert 'settings_desc_tts_engine' in src, (
            "TTS Engine description (settings_desc_tts_engine) missing"
        )
        # Must not contain the old "Edge TTS uses Microsoft neural voices" text
        # (describes the OLD direct-Edge-TTS architecture, not the delegated one)
        assert 'Edge TTS uses Microsoft neural voices' not in src, (
            "Description references the old direct-Edge-TTS architecture"
        )

    def test_voice_description_present(self):
        """Voice description must be set and accurate."""
        src = _read('index.html')
        assert 'settings_desc_tts_voice' in src

    def test_no_pitch_in_config_claim(self):
        """No setting description should claim pitch is in config.yaml."""
        src = _read('index.html')
        assert 'pitch is configured in config.yaml' not in src, (
            "Pitch description still claims it's in config.yaml — false, "
            "there's no general tts.pitch setting"
        )
