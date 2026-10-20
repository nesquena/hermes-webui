"""Regression guards for localized profile-concept help copy (#2147)."""

import re
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
I18N_JS = (ROOT / "static" / "i18n.js").read_text(encoding="utf-8")
PANELS_JS = (ROOT / "static" / "panels.js").read_text(encoding="utf-8")

PROFILE_CONCEPT_KEYS = [
    "profile_concept_title",
    "profile_concept_subtitle",
    "profile_concept_desc_profiles",
    "profile_concept_desc_workspaces",
    "profile_concept_desc_together",
    "profile_concept_example",
    "profile_concept_label_together",
    "profile_concept_label_example",
]


def _locale_blocks():
    """Extract every top-level locale block from static/i18n.js."""
    matches = list(re.finditer(r"^  ('[^']+'|[A-Za-z][A-Za-z0-9-]*): \{$", I18N_JS, re.MULTILINE))
    end = I18N_JS.index("\n};", matches[-1].start())
    return {
        match.group(1).strip("'"): I18N_JS[match.start() : (matches[index + 1].start() if index + 1 < len(matches) else end)]
        for index, match in enumerate(matches)
    }


def _render_profiles_panel_body():
    start = PANELS_JS.index("async function loadProfilesPanel(")
    end = PANELS_JS.index("\nfunction ", start + 1)
    return PANELS_JS[start:end]


def _render_profile_concept_help_body():
    start = PANELS_JS.index("function _renderProfileConceptHelp(")
    end = PANELS_JS.index("\nfunction ", start + 1)
    return PANELS_JS[start:end]


def test_i18n_keys_are_english_fallback_owned():
    """Profile concept keys live in English and fall back from every other
    locale. A locale may carry a real translation (#7579 added the Russian
    ones), but must never copy the English string verbatim — translate it or
    omit the key and inherit the fallback."""
    value_re = lambda block, key: re.search(
        rf"\b{re.escape(key)}:\s*'((?:[^'\\]|\\.)*)'", block
    )
    locale_blocks = _locale_blocks()
    en_block = locale_blocks["en"]
    for key in PROFILE_CONCEPT_KEYS:
        assert value_re(en_block, key), f"missing key {key!r} in en locale block"
    for locale, block in locale_blocks.items():
        if locale == "en":
            continue
        for key in PROFILE_CONCEPT_KEYS:
            match = value_re(block, key)
            if match is None:
                continue  # absent → inherits the English fallback
            en_match = value_re(en_block, key)
            assert match.group(1) != en_match.group(1), (
                f"key {key!r} in locale {locale!r} copies the English fallback "
                "verbatim — translate it or omit the key"
            )
    assert "_locale[key] ?? LOCALES.en[key]" in I18N_JS


def test_help_card_uses_i18n_keys():
    """The profiles panel explainer card must use t() instead of hardcoded English."""
    panel_body = _render_profiles_panel_body()
    assert "t('profile_concept_title')" in panel_body
    assert "t('profile_concept_subtitle')" in panel_body
    assert "Profiles vs workspaces" not in panel_body
    assert "Use profiles for how the agent works; use workspaces for what files it works on." not in panel_body


def test_concept_detail_uses_i18n_keys():
    """The concept detail view must use t() for the title and each description row."""
    detail_body = _render_profile_concept_help_body()
    assert "t('profile_concept_title')" in detail_body
    assert "t('profile_concept_desc_profiles')" in detail_body
    assert "t('profile_concept_desc_workspaces')" in detail_body
    assert "t('profile_concept_desc_together')" in detail_body
    assert "Profiles vs workspaces" not in detail_body


def test_example_row_present():
    """The concept detail view must render an example row."""
    detail_body = _render_profile_concept_help_body()
    assert "t('profile_concept_example')" in detail_body
