"""Regression tests for #7579: Russian locale gaps.

Two failure modes are covered:

1. Keys that existed in LOCALES.ru but still carried their English text
   ("untranslated in place") — e.g. the transparent-stream settings strings.
2. Keys missing from LOCALES.ru entirely and silently falling back to
   LOCALES.en via t() — e.g. the profile_concept_* block.

The assertions pin each key to a ru value that differs from the English
source text, so neither failure mode can regress without a failing test.
"""

import re

from tests.i18n_locale_loader import locale_string_value

# Keys that held English text inside the ru block before #7579.
RU_UNTRANSLATED_KEYS = [
    "settings_label_transparent_stream_event_timestamps",
    "settings_desc_transparent_stream_event_timestamps",
]

# Keys absent from the ru block before #7579 (en-only fallback via t()).
RU_MISSING_KEYS = [
    "profile_concept_title",
    "profile_concept_subtitle",
    "profile_concept_desc_profiles",
    "profile_concept_desc_workspaces",
    "profile_concept_desc_together",
    "profile_concept_example",
    "profile_concept_label_together",
    "profile_concept_label_example",
]


def _value_for(locale: str, key: str) -> str | None:
    return locale_string_value(locale, key)


def test_ru_transparent_stream_keys_are_translated():
    for key in RU_UNTRANSLATED_KEYS:
        ru_val = _value_for("ru", key)
        en_val = _value_for("en", key)
        assert ru_val, f"{key} missing from LOCALES.ru"
        assert en_val, f"{key} missing from LOCALES.en (fixture broken)"
        assert ru_val != en_val, (
            f"{key} still carries the English text in LOCALES.ru: {ru_val!r}"
        )


def test_ru_profile_concept_keys_exist_and_are_translated():
    for key in RU_MISSING_KEYS:
        ru_val = _value_for("ru", key)
        en_val = _value_for("en", key)
        assert ru_val, f"{key} missing from LOCALES.ru (en fallback would render English)"
        assert en_val, f"{key} missing from LOCALES.en (fixture broken)"
        assert ru_val != en_val, (
            f"{key} carries the English text in LOCALES.ru: {ru_val!r}"
        )


def test_ru_profile_concept_values_look_russian():
    # Spot-check: the translated block should contain Cyrillic text.
    for key in ("profile_concept_title", "profile_concept_subtitle"):
        ru_val = _value_for("ru", key)
        assert ru_val and re.search(r"[Ѐ-ӿ]", ru_val), (
            f"{key} value has no Cyrillic characters: {ru_val!r}"
        )
