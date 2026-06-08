"""Regression: the Settings language dropdown must list ALL locales in LOCALES.

#3539 (zh localization) briefly added an `allowed=['en','zh']` filter to the
language `<select>` population in loadSettingsPanel(). Because saveSettings()
falls back to 'en' when the select has no matching option, that filter would
silently reset an existing it/ja/ru/es/de/pt/ko/fr/tr user to English on the
next Settings save. The dropdown must keep enumerating every LOCALES entry;
partially-translated locales fall back per-key to English at render time, which
is the established behavior — far better than dropping the user's choice.
"""
from pathlib import Path

PANELS_JS = (Path(__file__).resolve().parents[1] / "static" / "panels.js").read_text(encoding="utf-8")


def _language_dropdown_block() -> str:
    # Anchor on the dropdown-population site (langSel.innerHTML='' precedes the
    # LOCALES enumeration); there is an earlier settingsLanguage reference for
    # the apply-on-load path, so don't anchor on the first match.
    i = PANELS_JS.index("langSel.innerHTML=''")
    return PANELS_JS[i:i + 700]


def test_language_dropdown_lists_all_locales_no_allowlist():
    block = _language_dropdown_block()
    # It must iterate every LOCALES entry...
    assert "Object.entries(LOCALES)" in block, (
        "the language dropdown must enumerate all LOCALES entries"
    )
    # ...with NO hardcoded allow-list filter that drops existing locales.
    assert "allowed=[" not in block.replace(" ", "") and "allowed = [" not in block, (
        "the language dropdown must NOT filter LOCALES to a hardcoded allow-list "
        "(that silently resets existing non-en/zh users to English on save)"
    )
    assert "if(!allowed.includes(code))" not in block.replace(" ", ""), (
        "no allow-list `continue` guard may skip locales in the dropdown"
    )


def test_language_change_applies_locale_instantly():
    """#3539 keeper: changing the dropdown applies the locale live, not just on save."""
    block = _language_dropdown_block()
    assert "setLocale(this.value)" in block.replace(" ", "").replace("\n", "") or "setLocale(this.value)" in block, (
        "language change should call setLocale() for instant apply"
    )
