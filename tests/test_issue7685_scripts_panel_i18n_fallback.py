"""Regression tests for #7685: the Scripts subtab's new copy lives in `en` only.

The #2316 Scripts subtab added six user-facing keys. The first re-gate
translated them into the non-English locale blocks, but it also pasted the
**English** source text into every locale that had no translation yet
(``scripts_no_directory``, ``scripts_load_failed``, and — for the locales
whose word for "scripts" is spelled like English — ``cron_tab_scripts`` and
``scripts_detail_empty_title``).

``docs/GUIDELINES.md`` section 8 ("Fallbacks and defaults are contracts")
says new user-facing copy goes into the ``en`` locale and rides the existing
fallback — English must not be pasted into every locale block. A pasted copy
is a second source of truth: when the English wording changes, the pasted
copy keeps serving the stale string and the two drift apart silently.

``t()`` already implements the fallback::

    const val = _locale[key] ?? LOCALES.en[key];

so removing the English copies is behaviour-preserving: every locale keeps
rendering exactly the same text it rendered before, because the fallback
supplies the identical English string. These tests pin both halves of that
claim:

1. No non-English locale block carries a Scripts key whose value equals the
   English source text (the paste is gone).
2. Every non-English locale still *renders* a non-empty string for each
   Scripts key (the fallback supplies it).

Test 2 is the one that would catch an over-correction: deleting the keys
without confirming the fallback still resolves them would turn the Scripts
tab into raw key names at runtime.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent
I18N_JS = (REPO / "static" / "i18n.js").read_text(encoding="utf-8")

# The locale blocks defined in static/i18n.js. `en` is the source of truth
# and is exempt from the no-English-paste rule by definition.
LOCALES = [
    "it",
    "ja",
    "ru",
    "es",
    "de",
    "zh",
    "zh-Hant",
    "pt",
    "ko",
    "fr",
    "cs",
    "tr",
    "pl",
    "vi",
]

# The six user-facing keys the Scripts subtab introduced.
SCRIPTS_KEYS = [
    "cron_tab_jobs",
    "cron_tab_scripts",
    "scripts_empty",
    "scripts_no_directory",
    "scripts_load_failed",
    "scripts_detail_empty_title",
    "scripts_detail_empty_sub",
]


def _extract_locale_block(src: str, locale_key: str) -> str:
    """Return the body of one locale block from static/i18n.js.

    Locale keys are declared either bare (``it: {``) or quoted
    (``'zh-Hant': {``), and the blocks are siblings of the same top-level
    object, so the block runs from its own opener line to the next locale's
    opener line. Working line-by-line keeps the extraction immune to the
    apostrophes that translated strings contain (Italian and French copy has
    plenty of them), which would otherwise derail a brace counter.
    """
    opener = re.compile(rf"^  (?:'{re.escape(locale_key)}'|{re.escape(locale_key)}): *\{{")
    opener_any = re.compile(r"^  (?:'[a-zA-Z-]+'|[a-zA-Z-]+): *\{")
    lines = src.split("\n")

    start = None
    for index, line in enumerate(lines):
        if opener.match(line):
            start = index
            break
    assert start is not None, f"{locale_key} locale block not found"

    end = len(lines)
    for index in range(start + 1, len(lines)):
        if opener_any.match(lines[index]):
            end = index
            break

    return "\n".join(lines[start + 1 : end])


def _locale_values(locale_key: str) -> dict[str, str]:
    """Return the plain string values of a locale block, keyed by name."""
    block = _extract_locale_block(I18N_JS, locale_key)
    values: dict[str, str] = {}
    for key, value in re.findall(
        r"['\"]?([A-Za-z][A-Za-z0-9_]*)['\"]?\s*:\s*'([^']*)'", block
    ):
        values[key] = value
    return values


EN_VALUES = _locale_values("en")


@pytest.mark.parametrize("locale_key", LOCALES)
def test_scripts_keys_are_not_pasted_english_in_locale_blocks(locale_key: str) -> None:
    """No locale block may hold a Scripts key whose value is the English text."""
    values = _locale_values(locale_key)
    for key in SCRIPTS_KEYS:
        if key not in values:
            # Absent is the intended state: t() falls back to LOCALES.en.
            continue
        assert values[key] != EN_VALUES[key], (
            f"{locale_key}.{key} carries the English source text verbatim "
            f"({EN_VALUES[key]!r}). New copy belongs in the `en` locale and "
            f"rides the existing fallback -- do not paste English into "
            f"every locale block (docs/GUIDELINES.md section 8)."
        )


@pytest.mark.parametrize("locale_key", LOCALES)
def test_scripts_keys_still_resolve_for_every_locale(locale_key: str) -> None:
    """Every locale still resolves every Scripts key to a non-empty string.

    This is the behaviour half of the fix: the fallback in t()
    (``_locale[key] ?? LOCALES.en[key]``) must supply the keys that are no
    longer present in the locale block, so the Scripts tab never renders a
    raw key name.
    """
    values = _locale_values(locale_key)
    for key in SCRIPTS_KEYS:
        resolved = values.get(key, EN_VALUES.get(key))
        assert resolved, (
            f"{locale_key}.{key} resolves to an empty string; the en fallback "
            f"must supply it."
        )
        assert resolved.strip(), (
            f"{locale_key}.{key} resolves to whitespace only; the en fallback "
            f"must supply it."
        )


def test_english_source_of_truth_defines_every_scripts_key() -> None:
    """The `en` locale still carries every Scripts key the UI asks for."""
    for key in SCRIPTS_KEYS:
        assert key in EN_VALUES, f"en.{key} is missing from static/i18n.js"
        assert EN_VALUES[key].strip(), f"en.{key} is empty"
