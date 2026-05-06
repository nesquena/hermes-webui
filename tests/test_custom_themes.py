"""Custom appearance theme regressions.

Covers server persistence/validation and the source hooks needed for the UI to
apply user-defined CSS variable tokens before and after settings load.
"""
import json
import re
import urllib.error
import urllib.request
from pathlib import Path

from tests._pytest_port import BASE

ROOT = Path(__file__).parent.parent
BOOT_JS = (ROOT / "static" / "boot.js").read_text(encoding="utf-8")
PANELS_JS = (ROOT / "static" / "panels.js").read_text(encoding="utf-8")
INDEX_HTML = (ROOT / "static" / "index.html").read_text(encoding="utf-8")
CONFIG_PY = (ROOT / "api" / "config.py").read_text(encoding="utf-8")


def _post(path, body):
    data = json.dumps(body).encode("utf-8")
    req = urllib.request.Request(
        BASE + path,
        data=data,
        headers={"Content-Type": "application/json"},
    )
    try:
        with urllib.request.urlopen(req, timeout=10) as r:
            return json.loads(r.read()), r.status
    except urllib.error.HTTPError as e:
        payload = e.read()
        return json.loads(payload or b"{}"), e.code


def _get(path):
    with urllib.request.urlopen(BASE + path, timeout=10) as r:
        return json.loads(r.read()), r.status


SAMPLE_THEME = {
    "id": "quiet-forest",
    "name": "Quiet Forest",
    "mode": "dark",
    "tokens": {
        "bg": "#101815",
        "sidebar": "#17231F",
        "surface": "#1E2E28",
        "text": "#E8F2EA",
        "muted": "#9DB7A8",
        "accent": "#8FBF9F",
        "accent_hover": "#B7D9C1",
        "accent_bg": "rgba(143,191,159,0.12)",
        "border": "#31463D",
        "input_bg": "rgba(232,242,234,0.05)",
        "hover_bg": "rgba(232,242,234,0.08)",
    },
}


def test_custom_theme_round_trips_through_settings_api():
    original, status = _get("/api/settings")
    assert status == 200
    snapshot = {
        "theme": original.get("theme"),
        "skin": original.get("skin"),
        "custom_theme_id": original.get("custom_theme_id"),
        "custom_themes": original.get("custom_themes", []),
    }
    try:
        saved, status = _post(
            "/api/settings",
            {
                "theme": "custom",
                "custom_theme_id": SAMPLE_THEME["id"],
                "custom_themes": [SAMPLE_THEME],
            },
        )
        assert status == 200
        assert saved["theme"] == "custom"
        assert saved["custom_theme_id"] == SAMPLE_THEME["id"]
        assert saved["custom_themes"] == [SAMPLE_THEME]

        reloaded, status = _get("/api/settings")
        assert status == 200
        assert reloaded["theme"] == "custom"
        assert reloaded["custom_theme_id"] == SAMPLE_THEME["id"]
        assert reloaded["custom_themes"] == [SAMPLE_THEME]
    finally:
        _post("/api/settings", snapshot)


def test_custom_theme_rejects_unsafe_token_values_and_unknown_tokens():
    original, status = _get("/api/settings")
    assert status == 200
    snapshot = {
        "theme": original.get("theme"),
        "skin": original.get("skin"),
        "custom_theme_id": original.get("custom_theme_id"),
        "custom_themes": original.get("custom_themes", []),
    }
    try:
        unsafe = {
            "id": "bad-theme",
            "name": "Bad Theme",
            "mode": "dark",
            "tokens": {
                "bg": "url(javascript:alert(1))",
                "made_up_token": "#ffffff",
                "accent": "#8FBF9F",
            },
        }
        saved, status = _post(
            "/api/settings",
            {
                "theme": "custom",
                "custom_theme_id": unsafe["id"],
                "custom_themes": [unsafe],
            },
        )
        assert status == 200
        assert saved.get("theme") != "custom"
        assert saved.get("custom_themes") == []
        assert saved.get("custom_theme_id") in (None, "")
    finally:
        _post("/api/settings", snapshot)


def test_custom_theme_source_hooks_exist_for_first_paint_and_runtime_apply():
    assert 'custom:1' in INDEX_HTML
    assert 'hermes-custom-theme-tokens' in INDEX_HTML
    assert 'dataset.theme=\'custom\'' in INDEX_HTML or 'dataset.theme="custom"' in INDEX_HTML

    assert 'function _applyCustomThemeTokens' in BOOT_JS
    assert 'hermes-custom-theme-tokens' in BOOT_JS
    assert "--" in BOOT_JS and "style.setProperty" in BOOT_JS

    assert 'id="customThemeEditor"' in INDEX_HTML
    assert 'customThemeName' in INDEX_HTML
    assert 'customThemeColorGrid' in INDEX_HTML
    assert 'function _saveCustomThemeFromUi' in PANELS_JS
    assert 'custom_themes' in PANELS_JS
    assert 'custom_theme_id' in PANELS_JS


def test_custom_theme_picker_is_card_based_and_editor_is_collapsed_until_add():
    assert 'id="customThemeEditor"' in INDEX_HTML
    assert 'hidden' in re.search(r'<div class="settings-field" id="customThemeEditor"[^>]*>', INDEX_HTML).group(0)
    assert 'id="customThemeAddCard"' in INDEX_HTML
    assert 'onclick="_openCustomThemeEditor()"' in INDEX_HTML
    assert 'data-custom-theme-card' in PANELS_JS
    assert 'function _renderCustomThemeCards' in PANELS_JS
    assert 'function _openCustomThemeEditor' in PANELS_JS
    assert 'function _closeCustomThemeEditor' in PANELS_JS
    assert "_closeCustomThemeEditor();" in re.search(r"function _saveCustomThemeFromUi\(\).*?\n\}", PANELS_JS, re.S).group(0)


def test_custom_theme_controls_live_under_renamed_themes_section():
    assert 'data-i18n="settings_label_theme">Mode</label>' in INDEX_HTML
    assert 'data-i18n="settings_label_skin">Themes</label>' in INDEX_HTML
    assert 'id="customThemePickerGrid"' in INDEX_HTML
    assert INDEX_HTML.index('id="skinPickerGrid"') < INDEX_HTML.index('id="customThemePickerGrid"') < INDEX_HTML.index('id="customThemeEditor"')
    assert INDEX_HTML.index('id="themePickerGrid"') < INDEX_HTML.index('id="skinPickerGrid"')
    assert "const grid=$('customThemePickerGrid');" in PANELS_JS
    assert "#customThemePickerGrid .theme-pick-btn" in BOOT_JS


def test_custom_theme_cards_have_delete_confirmation_and_no_static_custom_pick_button():
    assert 'data-theme-val="custom"' not in INDEX_HTML
    assert 'function _confirmDeleteCustomTheme' in PANELS_JS
    assert 'showConfirmDialog' in re.search(r"function _confirmDeleteCustomTheme\(.*?\).*?\n\}", PANELS_JS, re.S).group(0)
    assert 'data-delete-custom-theme' in PANELS_JS
    assert '_deleteCustomThemeFromUi(id)' in PANELS_JS


def test_custom_theme_editor_uses_polished_color_picker_controls_and_live_preview():
    assert 'id="customThemePreview"' in INDEX_HTML
    assert 'type="color"' in PANELS_JS
    assert 'data-custom-token-hex' in PANELS_JS
    assert 'function _updateCustomThemePreview' in PANELS_JS
    assert 'function _previewCustomThemeFromUi' in PANELS_JS
    assert '_applyCustomThemeTokens(_previewCustomThemeFromUi())' in PANELS_JS
    assert 'custom-theme-preview-swatch' in PANELS_JS
    assert 'custom-color-row' in PANELS_JS
    assert 'custom-color-chip' in PANELS_JS
    assert 'custom-color-value' in PANELS_JS
    assert 'customThemePickerStyles' in PANELS_JS
    assert '::-webkit-color-swatch' in PANELS_JS
    assert 'grid-template-columns:repeat(auto-fit,minmax(190px,1fr))' in INDEX_HTML
    assert 'custom-color-label' in PANELS_JS
    assert 'custom-color-swatch' in PANELS_JS
    assert 'custom-color-meta' in PANELS_JS
    assert 'custom-color-input' in PANELS_JS
    assert 'box-shadow:0 1px 0 var(--border-subtle)' in PANELS_JS
    assert 'linear-gradient(180deg,var(--surface-subtle),transparent)' in PANELS_JS


def test_custom_theme_cards_have_edit_button_and_prefill_existing_theme():
    assert 'data-edit-custom-theme' in PANELS_JS
    assert 'function _editCustomTheme' in PANELS_JS
    assert '_openCustomThemeEditor(theme.id)' in PANELS_JS
    assert '_customThemeEditingId=themeId' in PANELS_JS
    assert 'themes.find(t=>t&&t.id===themeId)' in PANELS_JS
    assert 'Save changes' in PANELS_JS
    assert '>Edit</span>' in PANELS_JS
    assert 'Create theme' in PANELS_JS


def test_sienna_skin_is_valid_on_both_client_and_server():
    assert 'sienna' in CONFIG_PY
    assert re.search(r'_SETTINGS_SKIN_VALUES\s*=\s*\{[^}]*"sienna"', CONFIG_PY, re.S)
