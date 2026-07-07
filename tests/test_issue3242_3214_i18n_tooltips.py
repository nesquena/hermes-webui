from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
BOOT_JS = (ROOT / "static" / "boot.js").read_text(encoding="utf-8")
I18N_JS = (ROOT / "static" / "i18n.js").read_text(encoding="utf-8")
SESSIONS_JS = (ROOT / "static" / "sessions.js").read_text(encoding="utf-8")

LOCALE_COUNT = 15


def test_raw_audio_active_recording_uses_dedicated_i18n_key():
    assert "voice_recording_active" in I18N_JS
    assert I18N_JS.count("voice_recording_active") == LOCALE_COUNT
    assert "on ? (_rawAudioMode ? 'voice_recording_active' : 'voice_dictate_active')" in BOOT_JS


def test_sidebar_lineage_tooltip_suffixes_are_localized():
    assert I18N_JS.count("session_lineage_toggle_hint") == LOCALE_COUNT
    assert I18N_JS.count("session_lineage_static_hint") == LOCALE_COUNT
    assert "t('session_lineage_toggle_hint', base)" in SESSIONS_JS
    assert "t('session_lineage_static_hint', base)" in SESSIONS_JS
    assert "earlier context turns are collapsed here. Click to show or hide them" not in SESSIONS_JS


def test_sidebar_child_tooltip_suffix_is_localized():
    assert I18N_JS.count("session_child_toggle_hint") == LOCALE_COUNT
    assert "t('session_child_toggle_hint', base)" in SESSIONS_JS
    assert "child conversations spawned from this session. Click to show or hide them" not in SESSIONS_JS


def test_read_only_session_title_hint_is_localized():
    assert I18N_JS.count("session_readonly_title_hint") == LOCALE_COUNT
    assert "function _sessionFullTitleTooltip(rawTitle, cleanTitle, session)" in SESSIONS_JS
    assert "_isReadOnlySession(session)" in SESSIONS_JS
    assert "t('session_readonly_title_hint', title)" in SESSIONS_JS
    assert "title.title=_sessionFullTitleTooltip(rawTitle,cleanTitle,s);" in SESSIONS_JS
