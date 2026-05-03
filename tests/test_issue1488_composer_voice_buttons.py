"""Tests for #1488 — composer voice buttons (dictation vs voice mode).

The composer footer shows two voice-related buttons that look identical and
share the same tooltip ("Voice input") in master. This module pins the fix:

1. The buttons MUST have distinct, descriptive tooltips bound to i18n keys.
2. The voice-mode button MUST use the audio-lines (waveform) icon — the
   industry-standard glyph for two-way voice conversation, matching ChatGPT
   and Gemini.
3. The voice-mode button MUST be hidden by default and surface behind a
   Preferences toggle so the default composer footer stays uncluttered.
4. The dictation button (the older feature) MUST stay visible by default,
   unchanged.
5. All four new i18n keys (active and idle states for both buttons) MUST
   exist in every locale.
6. The legacy `voice_toggle` i18n key MUST be removed everywhere — its
   string was identical to the dictation tooltip and caused the bug.
"""
import re


def _src(name: str) -> str:
    with open(f"static/{name}") as f:
        return f.read()


class TestComposerVoiceButtonHTML:
    """index.html composer markup for the dictation + voice-mode buttons."""

    def test_dictation_button_has_dictate_i18n_key(self):
        """btnMic must bind data-i18n-title="voice_dictate" so its tooltip
        is distinct from the voice-mode button and localizable."""
        html = _src("index.html")
        m = re.search(
            r'<button[^>]*\bid="btnMic"[^>]*>',
            html,
            re.DOTALL,
        )
        assert m, "btnMic <button> tag must exist"
        tag = m.group(0)
        assert 'data-i18n-title="voice_dictate"' in tag, \
            "btnMic must have data-i18n-title=\"voice_dictate\" — without " \
            "it the tooltip stays as the static fallback and ignores locale."
        # Static fallback should also match (read by users with stale i18n)
        assert 'title="Dictate"' in tag, \
            "btnMic static title fallback must say 'Dictate' (not 'Voice input')."

    def test_voice_mode_button_has_voice_mode_i18n_key(self):
        """btnVoiceMode must bind data-i18n-title="voice_mode_toggle"."""
        html = _src("index.html")
        m = re.search(
            r'<button[^>]*\bid="btnVoiceMode"[^>]*>',
            html,
            re.DOTALL,
        )
        assert m, "btnVoiceMode <button> tag must exist"
        tag = m.group(0)
        assert 'data-i18n-title="voice_mode_toggle"' in tag, \
            "btnVoiceMode must use data-i18n-title=\"voice_mode_toggle\". " \
            "The legacy key 'voice_toggle' resolved to 'Voice input' and " \
            "made btnMic and btnVoiceMode appear identical."
        assert 'voice_toggle"' not in tag, \
            "Stale voice_toggle reference still on btnVoiceMode — must be voice_mode_toggle."

    def test_buttons_have_distinct_static_titles(self):
        """The static title attributes must differ as a fallback for users
        whose i18n hasn't loaded yet (e.g. very early page load)."""
        html = _src("index.html")
        mic = re.search(r'<button[^>]*\bid="btnMic"[^>]*>', html, re.DOTALL)
        vm = re.search(r'<button[^>]*\bid="btnVoiceMode"[^>]*>', html, re.DOTALL)
        assert mic and vm
        mic_title = re.search(r'\btitle="([^"]+)"', mic.group(0)).group(1)
        vm_title = re.search(r'\btitle="([^"]+)"', vm.group(0)).group(1)
        assert mic_title != vm_title, \
            f"Static titles must differ; both say {mic_title!r}"
        assert "voice input" not in mic_title.lower(), \
            f"btnMic static title still says 'Voice input': {mic_title!r}"
        assert "voice input" not in vm_title.lower(), \
            f"btnVoiceMode static title still says 'Voice input': {vm_title!r}"

    def test_voice_mode_uses_audio_lines_glyph(self):
        """btnVoiceMode SVG must use the audio-lines (waveform) shape.
        We detect the pattern by looking for the 6 vertical-bar paths
        characteristic of Lucide's audio-lines icon."""
        html = _src("index.html")
        # Extract the full button (open tag through </button>)
        m = re.search(
            r'<button[^>]*\bid="btnVoiceMode"[^>]*>(.+?)</button>',
            html,
            re.DOTALL,
        )
        assert m, "btnVoiceMode element must be parseable"
        body = m.group(1)
        # Lucide audio-lines path data — six <path d="M{x} {y}v{h}"/> entries.
        bars = re.findall(r'<path d="M\d+\s+\d+v\d+"', body)
        assert len(bars) >= 5, (
            f"btnVoiceMode SVG must use audio-lines (>=5 vertical-bar paths); "
            f"found {len(bars)}. Visual confusion bug returns if reverted to "
            f"the old 'mic with sparkles' shape."
        )
        # Must NOT contain the old mic-shaped rect (rx="3" capsule) — that's
        # the dictation glyph and using it again recreates #1488.
        assert 'rect x="9" y="1" width="6" height="12" rx="3"' not in body, \
            "btnVoiceMode regressed to mic shape — the visual confusion bug returns."


class TestComposerVoiceButtonI18n:
    """i18n.js must define the four new keys and remove the stale voice_toggle."""

    REQUIRED_KEYS = (
        "voice_dictate",
        "voice_dictate_active",
        "voice_mode_toggle",
        "voice_mode_toggle_active",
    )

    LOCALES = ("en", "ja", "ru", "es", "de", "zh", "zh-Hant", "pt", "ko")

    def test_legacy_voice_toggle_key_removed(self):
        """The old key whose string was 'Voice input' caused the duplicate-
        tooltip bug. It must no longer appear in i18n.js."""
        src = _src("i18n.js")
        # Match the property name only (not strings that happen to mention it).
        leftover = re.findall(r'\bvoice_toggle\s*:', src)
        assert not leftover, (
            f"Stale voice_toggle: key still in i18n.js ({len(leftover)} "
            f"occurrences). Replace with voice_mode_toggle / voice_dictate."
        )

    def test_all_locales_define_new_keys(self):
        """Every locale block must define all 4 new composer voice-button keys."""
        src = _src("i18n.js")
        for key in self.REQUIRED_KEYS:
            count = len(re.findall(rf'\b{re.escape(key)}\s*:', src))
            assert count == len(self.LOCALES), (
                f"i18n key {key!r} appears {count} times — expected one per "
                f"locale ({len(self.LOCALES)} locales: {self.LOCALES}). "
                f"Each locale block must define all four composer voice keys."
            )

    def test_english_dictate_label_is_dictate(self):
        """English voice_dictate must read 'Dictate' (not 'Voice input')."""
        src = _src("i18n.js")
        # Find the en block (first occurrence of voice_dictate is in en)
        m = re.search(r"\bvoice_dictate\s*:\s*'([^']+)'", src)
        assert m, "voice_dictate key not found"
        assert m.group(1) == "Dictate", \
            f"English voice_dictate should be 'Dictate'; got {m.group(1)!r}"

    def test_english_voice_mode_label_is_voice_mode(self):
        """English voice_mode_toggle must read 'Voice mode' — matches
        ChatGPT/Gemini convention (industry-standard label)."""
        src = _src("i18n.js")
        # Find the FIRST voice_mode_toggle in the file (en block) but skip
        # _active suffix variant — use a lookahead to assert no _active.
        m = re.search(r"\bvoice_mode_toggle\s*:\s*'([^']+)'", src)
        assert m, "voice_mode_toggle key not found"
        assert m.group(1) == "Voice mode", \
            f"English voice_mode_toggle should be 'Voice mode'; got {m.group(1)!r}"


class TestVoiceModePreferenceGate:
    """boot.js must hide btnVoiceMode by default, surface it via Preferences."""

    def test_voice_mode_pref_is_localstorage_backed(self):
        """The pref reads from localStorage key 'hermes-voice-mode-button'."""
        src = _src("boot.js")
        assert "'hermes-voice-mode-button'" in src, (
            "boot.js must read/write the localStorage key 'hermes-voice-mode-button' "
            "for the voice-mode visibility pref."
        )

    def test_voice_mode_button_hidden_until_pref_enabled(self):
        """Default state of btnVoiceMode display must be 'none'; visibility
        gated by the pref check, not unconditional."""
        src = _src("boot.js")
        # Find the voice-mode pref helper. Must NOT contain an
        # unconditional `modeBtn.style.display='';` (the master bug).
        # Instead, the function _applyVoiceModePref must be the source of truth.
        assert "_applyVoiceModePref" in src, \
            "boot.js must expose _applyVoiceModePref so settings toggle re-applies live."
        assert "_voiceModePrefEnabled" in src, \
            "boot.js must define _voiceModePrefEnabled to read the pref."
        # The pre-existing `modeBtn.style.display='';` line must be gone.
        # We allow `style.display = _voiceModePrefEnabled() ? '' : 'none'`.
        assert "modeBtn.style.display='';" not in src, (
            "boot.js still contains unconditional `modeBtn.style.display='';` — "
            "this defeats the Preferences gate and reintroduces #1488."
        )

    def test_settings_pane_has_voice_mode_checkbox(self):
        """index.html Preferences pane must include the toggle checkbox."""
        html = _src("index.html")
        assert 'id="settingsVoiceModeEnabled"' in html, \
            "Preferences pane must include #settingsVoiceModeEnabled checkbox."
        assert 'data-i18n="settings_label_voice_mode"' in html, \
            "Voice-mode pref label must use data-i18n='settings_label_voice_mode'."
        assert 'data-i18n="settings_desc_voice_mode"' in html, \
            "Voice-mode pref description must use data-i18n='settings_desc_voice_mode'."

    def test_settings_pane_has_voice_mode_i18n_keys(self):
        """The two new pref-label i18n keys must exist in every locale."""
        src = _src("i18n.js")
        for key in ("settings_label_voice_mode", "settings_desc_voice_mode"):
            count = len(re.findall(rf'\b{re.escape(key)}\s*:', src))
            assert count == 9, (
                f"Preferences i18n key {key!r} appears {count} times — "
                f"expected 9 (one per locale)."
            )

    def test_panels_js_wires_voice_mode_pref(self):
        """panels.js must read the checkbox state, persist to localStorage,
        and call _applyVoiceModePref so the change is live without reload."""
        src = _src("panels.js")
        assert "settingsVoiceModeEnabled" in src, \
            "panels.js must reference the #settingsVoiceModeEnabled checkbox."
        assert "'hermes-voice-mode-button'" in src, \
            "panels.js must persist the pref to localStorage key 'hermes-voice-mode-button'."
        assert "_applyVoiceModePref" in src, \
            "panels.js onchange handler must call window._applyVoiceModePref() " \
            "so the button appears/disappears immediately."


class TestActiveStateTooltips:
    """When recording / in voice mode, tooltips should flip to the
    'stop' variants so the affordance is honest."""

    def test_dictation_active_tooltip_changes_when_recording(self):
        """_setRecording(on) should flip btnMic.title to voice_dictate_active."""
        src = _src("boot.js")
        m = re.search(r"function _setRecording\(on\)\{.*?\n  \}", src, re.DOTALL)
        assert m, "_setRecording function must exist"
        body = m.group(0)
        assert "voice_dictate_active" in body, (
            "_setRecording must flip the tooltip to voice_dictate_active when "
            "recording starts so the user knows pressing it now stops dictation."
        )
        assert "voice_dictate'" in body or "voice_dictate\"" in body, \
            "_setRecording must restore voice_dictate when recording stops."

    def test_voice_mode_active_tooltip(self):
        """_activate() should set modeBtn.title to voice_mode_toggle_active."""
        src = _src("boot.js")
        m = re.search(r"function _activate\(\)\{.*?\n  \}", src, re.DOTALL)
        assert m, "_activate function must exist"
        body = m.group(0)
        assert "voice_mode_toggle_active" in body, (
            "_activate must flip the tooltip to voice_mode_toggle_active so "
            "the next click obviously exits voice mode."
        )

    def test_voice_mode_idle_tooltip(self):
        """_deactivate() should set modeBtn.title back to voice_mode_toggle."""
        src = _src("boot.js")
        m = re.search(r"function _deactivate\(\)\{.*?\n  \}", src, re.DOTALL)
        assert m, "_deactivate function must exist"
        body = m.group(0)
        assert re.search(r"voice_mode_toggle['\"]", body), (
            "_deactivate must restore voice_mode_toggle (idle title) when "
            "the user exits voice mode."
        )


class TestAudioLinesIconRegistered:
    """The audio-lines icon should be in LI_PATHS for any future reuse via li()."""

    def test_audio_lines_in_li_paths(self):
        src = _src("icons.js")
        assert "'audio-lines'" in src, \
            "audio-lines must be registered in LI_PATHS for li('audio-lines') reuse."
