"""
Tests for #499: TTS playback of agent responses via Web Speech API.

Verifies that TTS utility functions, speaker button rendering, and
settings controls are present in the WebUI codebase.
"""
import os
import re

STATIC_DIR = os.path.join(os.path.dirname(__file__), '..', 'static')


def _read(filename):
    return open(os.path.join(STATIC_DIR, filename), encoding='utf-8').read()


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
