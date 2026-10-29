"""Regression coverage for issue #8090: i18n bundle parity gaps.

The profiles panel explains itself in English for every non-English user
because the ``profile_concept_*`` keys only exist in the ``en`` bundle
(``ru`` was filled by #8001; the other 12 locales were not). The
project-filter quick-create (+) button also hardcodes its tooltip and
``aria-label`` in English, and the Russian bundle kept several English
placeholder strings for user-visible UI.

This test parses the ``LOCALES`` object in ``static/i18n.js`` directly and
asserts, per code-used key, that every non-``en`` bundle defines it. Keys
that are deliberately English (placeholders, brand/technical labels) are
excluded explicitly, as requested in the #8090 maintainer reply.
"""
from __future__ import annotations

import re
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
I18N_JS = (REPO / "static" / "i18n.js").read_text(encoding="utf-8")
SESSIONS_JS = (REPO / "static" / "sessions.js").read_text(encoding="utf-8")


def _locale_blocks(src: str) -> dict[str, str]:
    """Split the LOCALES object into {locale: block-text}.

    Covers both plain keys (``zh: {``) and the quoted ``'zh-Hant': {``.
    """
    starts = [
        (m.group(1), m.start())
        for m in re.finditer(r"^  '?([A-Za-z][A-Za-z-]*)'?: \{", src, re.M)
    ]
    blocks: dict[str, str] = {}
    for i, (name, start) in enumerate(starts):
        end = starts[i + 1][1] if i + 1 < len(starts) else len(src)
        blocks[name] = src[start:end]
    return blocks


BLOCKS = _locale_blocks(I18N_JS)
LOCALES = sorted(BLOCKS)
KEY_RE = {loc: set(re.findall(r"^\s*([a-z0-9_]+)\s*:", block, re.M)) for loc, block in BLOCKS.items()}

# Keys that must exist in every bundle because code calls t() on them.
CODE_USED_KEYS = [
    "profile_concept_title",
    "profile_concept_subtitle",
    "profile_concept_desc_profiles",
    "profile_concept_desc_workspaces",
    "profile_concept_desc_together",
    "profile_concept_example",
    "profile_concept_label_together",
    "profile_concept_label_example",
    "project_chip_all",
    "project_chip_unassigned",
    "project_chip_unassigned_title",
    "project_chip_new_in_project",
]


def test_locale_blocks_discovered():
    """Sanity: all 15 bundles (14 + zh-Hant) parse out of i18n.js."""
    assert len(LOCALES) == 15, f"expected 15 locale bundles, found {len(LOCALES)}: {LOCALES}"


def test_code_used_keys_exist_in_every_bundle():
    """Every code-used key above must be defined in every non-en bundle
    (en is the source of truth and defines them all by construction)."""
    missing = {
        loc: [k for k in CODE_USED_KEYS if k not in KEY_RE[loc]]
        for loc in LOCALES
    }
    missing = {loc: keys for loc, keys in missing.items() if keys}
    assert not missing, f"code-used keys missing from bundles: {missing}"


def test_quick_create_button_is_localized():
    """The project-chip quick-create (+) button must route its title and
    aria-label through t() instead of hardcoding English."""
    assert "btn.title='New conversation in this project'" not in SESSIONS_JS
    assert "btn.setAttribute('aria-label','New conversation in this project')" not in SESSIONS_JS
    assert "t('project_chip_new_in_project')" in SESSIONS_JS


def test_russian_bundle_translates_visible_placeholders():
    """The Russian bundle must not ship English text for these visible UI
    strings (audit table in #8090 section 3)."""
    ru = BLOCKS["ru"]

    def ru_value(key: str) -> str:
        m = re.search(rf"^\s*{key}:\s*'((?:[^'\\]|\\.)*)'", ru, re.M)
        assert m, f"ru bundle is missing {key}"
        return m.group(1)

    expectations = {
        "insights_period_7d": "7 дней",
        "insights_period_30d": "30 дней",
        "insights_period_90d": "90 дней",
        "insights_period_365d": "365 дней",
        "kanban_workspace_worktree": "Рабочее дерево",
        "session_worktree_badge": "Рабочее дерево",
        "kanban_bulk_status_aria": "Массовое изменение статуса",
    }
    for key, expected in expectations.items():
        assert ru_value(key) == expected, (
            f"ru {key} should be {expected!r}, got {ru_value(key)!r}"
        )


def test_profile_concept_values_differ_from_english():
    """Non-en bundles must carry real translations (not copied English)
    for the profile-concept keys used by the profiles panel."""

    def value(loc: str, key: str) -> str | None:
        m = re.search(rf"^\s*{key}:\s*'((?:[^'\\]|\\.)*)'", BLOCKS[loc], re.M)
        return m.group(1) if m else None

    en_val = value("en", "profile_concept_title")
    assert en_val == "Profiles vs workspaces"
    for loc in LOCALES:
        if loc == "en":
            continue
        v = value(loc, "profile_concept_title")
        assert v and v != en_val, f"{loc} profile_concept_title is untranslated: {v!r}"
