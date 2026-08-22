"""Static contracts for Matrix session visibility controls."""

import pathlib


ROOT = pathlib.Path(__file__).resolve().parent.parent


def _read(relative):
    return (ROOT / relative).read_text(encoding="utf-8")


def test_matrix_visibility_checkbox_is_present_and_localized():
    html = _read("static/index.html")

    assert 'id="settingsShowMatrixSessions"' in html
    assert 'data-i18n="settings_label_matrix_sessions"' in html
    assert 'data-i18n="settings_desc_matrix_sessions"' in html


def test_matrix_visibility_is_saved_on_both_settings_paths():
    panels = _read("static/panels.js")

    assert "const showMatrixCb=$('settingsShowMatrixSessions')" in panels
    assert "payload.show_matrix_sessions=!!(showCliCb&&showCliCb.checked&&showMatrixCb.checked)" in panels
    assert "body.show_matrix_sessions=showCliSessions&&showMatrixSessions" in panels
    assert "settings.show_matrix_sessions" in panels


def test_matrix_visibility_is_subordinate_to_external_sessions():
    panels = _read("static/panels.js")

    assert "showMatrixCb.disabled=!showCliCb.checked" in panels
    assert "if(showMatrixCb) showMatrixCb.disabled=!enabled" in panels


def test_matrix_source_label_and_messaging_classification_are_preserved():
    sessions = _read("static/sessions.js")

    assert "'matrix'" in sessions
    assert "matrix: 'Matrix'" in sessions
    assert "session.session_source === 'messaging'" in sessions


def test_matrix_preference_keys_exist_in_every_locale_block():
    i18n = _read("static/i18n.js")

    locale_count = i18n.count("settings_label_external_sessions:")
    assert locale_count >= 9
    assert i18n.count("settings_label_matrix_sessions:") == locale_count
    assert i18n.count("settings_desc_matrix_sessions:") == locale_count

